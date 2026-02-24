"""
Beat Claude - AI Hiring Companion
Backend with session auth, SQLite database, and Ollama AI integration
"""
from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import sqlite3
import json
import csv
import io
import re
import httpx
import os
import bcrypt
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv

load_dotenv()

# Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-in-production")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b-instruct-q4_K_M")
DB_PATH = os.getenv("DB_PATH", "beat_claude.db")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# Rate limiting for scoring endpoint
_score_rate_limit: Dict[int, float] = {}
SCORE_RATE_LIMIT_SECONDS = 10

# Create FastAPI app
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Beat Claude API", version="1.0.0", lifespan=lifespan)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=3600 * 24  # 24 hours
)

# ============================================================================
# DATABASE
# ============================================================================

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initialize database tables"""
    conn = get_db()
    cursor = conn.cursor()

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            company_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Assessments table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            role_title TEXT NOT NULL,
            seniority_level TEXT,
            department TEXT,
            domain TEXT,
            experience TEXT,
            education TEXT,
            required_skills TEXT,
            preferred_skills TEXT,
            tools TEXT,
            responsibilities TEXT,
            soft_skills TEXT,
            raw_jd TEXT,
            duration_minutes INTEGER DEFAULT 60,
            status TEXT DEFAULT 'draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            closed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Assessments index
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assessments_user ON assessments(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_assessments_status ON assessments(status)")

    # Questions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL,
            question_number INTEGER NOT NULL,
            type TEXT NOT NULL,
            skill TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            question_text TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT,
            guidelines TEXT NOT NULL,
            max_score INTEGER DEFAULT 10,
            FOREIGN KEY (assessment_id) REFERENCES assessments(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_questions_assessment ON questions(assessment_id)")

    # Candidates table with UNIQUE constraint on (assessment_id, email)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            started_at TIMESTAMP,
            submitted_at TIMESTAMP,
            time_spent INTEGER,
            has_submitted INTEGER DEFAULT 0,
            scoring_status TEXT DEFAULT 'pending',
            total_score REAL DEFAULT 0,
            max_score REAL DEFAULT 0,
            percentage REAL DEFAULT 0,
            rank INTEGER,
            percentile REAL,
            recommendation TEXT,
            ai_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assessment_id) REFERENCES assessments(id) ON DELETE CASCADE,
            UNIQUE(assessment_id, email)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_candidates_assessment ON candidates(assessment_id)")

    # Responses table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            answer_text TEXT,
            selected_option TEXT,
            score REAL DEFAULT 0,
            reasoning TEXT,
            strengths TEXT,
            gaps TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
        )
    """)

    conn.commit()

    # ── Schema migrations ────────────────────────────────────────────────────
    # Safely add any columns that were introduced in later versions.
    # SQLite doesn't support IF NOT EXISTS on ALTER TABLE, so we check first.
    def existing_columns(table):
        cursor.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in cursor.fetchall()}

    # candidates table – columns added in v2 rewrite
    cand_cols = existing_columns("candidates")
    migrations_candidates = [
        ("scoring_status",  "TEXT DEFAULT 'pending'"),
        ("total_score",     "REAL DEFAULT 0"),
        ("max_score",       "REAL DEFAULT 0"),
        ("percentage",      "REAL DEFAULT 0"),
        ("rank",            "INTEGER"),
        ("percentile",      "REAL"),
        ("recommendation",  "TEXT"),
        ("ai_summary",      "TEXT"),
        ("time_spent",      "INTEGER"),
        ("started_at",      "TIMESTAMP"),
        ("submitted_at",    "TIMESTAMP"),
        ("notes",           "TEXT DEFAULT ''"),
    ]
    for col, col_def in migrations_candidates:
        if col not in cand_cols:
            cursor.execute(f"ALTER TABLE candidates ADD COLUMN {col} {col_def}")
            print(f"  ↳ Migration: added candidates.{col}")

    # responses table – columns added in v2 rewrite
    resp_cols = existing_columns("responses")
    migrations_responses = [
        ("selected_option", "TEXT"),
        ("strengths",       "TEXT"),
        ("gaps",            "TEXT"),
        ("reasoning",       "TEXT"),
    ]
    for col, col_def in migrations_responses:
        if col not in resp_cols:
            cursor.execute(f"ALTER TABLE responses ADD COLUMN {col} {col_def}")
            print(f"  ↳ Migration: added responses.{col}")

    # questions table – columns added in v2 rewrite
    q_cols = existing_columns("questions")
    migrations_questions = [
        ("guidelines",      "TEXT NOT NULL DEFAULT ''"),
        ("correct_answer",  "TEXT"),
        ("difficulty",      "TEXT NOT NULL DEFAULT 'medium'"),
        ("skill",           "TEXT NOT NULL DEFAULT 'General'"),
    ]
    for col, col_def in migrations_questions:
        if col not in q_cols:
            cursor.execute(f"ALTER TABLE questions ADD COLUMN {col} {col_def}")
            print(f"  ↳ Migration: added questions.{col}")

    conn.commit()
    conn.close()
    print("✅ Database initialized!")


# ============================================================================
# SECURITY HELPERS
# ============================================================================

def strip_prompt_injection(text: str) -> str:
    """Strip common prompt injection patterns from user input"""
    if not text:
        return text
    # Remove common injection patterns
    patterns = [
        r'ignore\s+(previous|above|all)\s+instructions?',
        r'(system|assistant|user)\s*:\s*',
        r'<\s*(system|assistant|user)\s*>',
        r'\[\s*(INST|SYS|END)\s*\]',
        r'###\s*(instruction|system|prompt)',
        r'act\s+as\s+(if|though)',
        r'new\s+role',
        r'forget\s+(your|all|previous)',
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, '[REMOVED]', cleaned, flags=re.IGNORECASE)
    return cleaned[:10000]  # Hard limit


# ============================================================================
# AI SERVICE
# ============================================================================

@retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 10))
async def call_ollama(prompt: str, system: str = "") -> str:
    """Call Ollama API with retry logic"""
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "top_p": 0.8
                }
            }
        )
        response.raise_for_status()
        return response.json()["response"]


async def parse_job_description(jd_text: str) -> dict:
    """Parse job description using AI"""
    system = (
        "You are an expert HR assistant. Extract structured information from job descriptions. "
        "Return ONLY valid JSON. Do not include any commentary, markdown, or explanations."
    )

    prompt = f"""Parse this job description and extract structured information.

JOB DESCRIPTION:
{jd_text}

Return ONLY this JSON object (no markdown, no explanation):
{{
    "role_title": "Job title",
    "seniority_level": "entry/junior/mid/senior/lead",
    "department": "Department name",
    "domain": "Industry domain",
    "years_of_experience_required": "Experience range",
    "education_requirements": "Education level",
    "required_skills": ["skill1", "skill2"],
    "preferred_skills": ["skill1", "skill2"],
    "tools_technologies": ["tool1", "tool2"],
    "key_responsibilities": ["responsibility1", "responsibility2"],
    "soft_skills": ["skill1", "skill2"]
}}

Use "NOT SPECIFIED" for missing string fields and [] for missing array fields."""

    try:
        response = await call_ollama(prompt, system)
        start = response.find('{')
        end = response.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(response[start:end])
        return json.loads(response)
    except Exception as e:
        print(f"Error parsing JD: {e}")
        return {
            "role_title": "NOT SPECIFIED",
            "seniority_level": "NOT SPECIFIED",
            "department": "NOT SPECIFIED",
            "domain": "NOT SPECIFIED",
            "years_of_experience_required": "NOT SPECIFIED",
            "education_requirements": "NOT SPECIFIED",
            "required_skills": [],
            "preferred_skills": [],
            "tools_technologies": [],
            "key_responsibilities": [],
            "soft_skills": []
        }


async def generate_questions(jd_data: dict, num_questions: int = 10) -> list:
    """Generate assessment questions using AI"""
    system = (
        "You are an expert technical interviewer. Create high-quality interview questions. "
        "Return ONLY valid JSON. No markdown, no commentary."
    )

    skills = ", ".join(jd_data.get("required_skills", [])[:6])
    seniority = jd_data.get("seniority_level", "mid").lower()

    # Adjust ratio based on seniority
    if seniority in ["senior", "lead", "principal"]:
        ratio = "20% MCQ, 30% SHORT_ANSWER, 50% SCENARIO"
    elif seniority in ["junior", "entry"]:
        ratio = "50% MCQ, 30% SHORT_ANSWER, 20% SCENARIO"
    else:
        ratio = "30% MCQ, 30% SHORT_ANSWER, 40% SCENARIO"

    prompt = f"""Generate exactly {num_questions} interview questions for this role.

ROLE: {jd_data.get('role_title', 'Unknown')}
SENIORITY: {seniority}
REQUIRED SKILLS: {skills}
RESPONSIBILITIES: {', '.join(jd_data.get('key_responsibilities', [])[:3])}

Create a mix: {ratio}

For MCQ questions, options must be an array of strings like ["Option A text", "Option B text", "Option C text", "Option D text"]
For SHORT_ANSWER and SCENARIO, options must be []
correct_answer for MCQ must be "A", "B", "C", or "D" (letter only)
correct_answer for SHORT_ANSWER/SCENARIO must be ""

Return ONLY this JSON array, no markdown:
[
    {{
        "type": "MCQ",
        "skill_tested": "specific skill name",
        "difficulty_level": "easy",
        "question_text": "The complete question text?",
        "options": ["First option", "Second option", "Third option", "Fourth option"],
        "correct_answer": "A",
        "ideal_answer_guidelines": "Why A is correct and what to look for",
        "max_score": 10
    }},
    {{
        "type": "SHORT_ANSWER",
        "skill_tested": "specific skill name",
        "difficulty_level": "medium",
        "question_text": "The complete question text?",
        "options": [],
        "correct_answer": "",
        "ideal_answer_guidelines": "Key points the ideal answer should cover",
        "max_score": 10
    }}
]"""

    try:
        response = await call_ollama(prompt, system)
        start = response.find('[')
        end = response.rfind(']') + 1
        if start >= 0 and end > start:
            questions = json.loads(response[start:end])
        else:
            questions = json.loads(response)
        # Validate and clean questions
        clean = []
        for q in questions:
            if not q.get("question_text"):
                continue
            q["type"] = q.get("type", "SHORT_ANSWER").upper()
            if q["type"] not in ["MCQ", "SHORT_ANSWER", "SCENARIO"]:
                q["type"] = "SHORT_ANSWER"
            if q["type"] != "MCQ":
                q["options"] = []
                q["correct_answer"] = ""
            q["max_score"] = int(q.get("max_score", 10))
            clean.append(q)
        return clean
    except Exception as e:
        print(f"Error generating questions: {e}")
        return []


async def score_response(question: str, guidelines: str, answer: str, q_type: str, max_score: int) -> dict:
    """Score a candidate response using AI"""
    system = (
        "You are an expert technical interviewer. Score responses objectively and consistently. "
        "Return ONLY valid JSON. No commentary."
    )

    # Sanitize
    answer = strip_prompt_injection(answer or "")

    prompt = f"""Score this candidate response strictly based on the guidelines.

QUESTION: {question}
QUESTION TYPE: {q_type}
IDEAL ANSWER GUIDELINES: {guidelines}
CANDIDATE'S ANSWER: {answer if answer else "(no answer provided)"}
MAXIMUM SCORE: {max_score}

Rules:
- Be strict. No points for empty or irrelevant answers.
- Score must be between 0 and {max_score}.
- Same quality answer always gets same score.
- Identify specific strengths and gaps.

Return ONLY this JSON:
{{
    "score": <number 0 to {max_score}>,
    "reasoning": "Detailed 1-2 sentence explanation",
    "strengths": ["specific strength 1", "specific strength 2"],
    "gaps": ["specific gap 1", "specific gap 2"]
}}"""

    for attempt in range(3):
        try:
            response = await call_ollama(prompt, system)
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(response[start:end])
            else:
                result = json.loads(response)

            # Validate schema
            score = float(result.get("score", 0))
            score = max(0.0, min(float(max_score), score))
            return {
                "score": score,
                "reasoning": str(result.get("reasoning", "")),
                "strengths": list(result.get("strengths", [])),
                "gaps": list(result.get("gaps", []))
            }
        except Exception as e:
            print(f"Score attempt {attempt+1} failed: {e}")
            if attempt == 2:
                break

    return {
        "score": 0,
        "reasoning": "Error during AI scoring",
        "strengths": [],
        "gaps": ["Could not evaluate response"]
    }


# ============================================================================
# AUTH HELPERS
# ============================================================================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_current_user(request: Request) -> Optional[dict]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, full_name, company_name FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()

    return dict(user) if user else None


# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def auto_score_candidate(candidate_id: int):
    """Background task: AI score all non-MCQ responses for a candidate"""
    import asyncio
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Mark scoring as in progress
        cursor.execute("UPDATE candidates SET scoring_status = 'scoring' WHERE id = ?", (candidate_id,))
        conn.commit()

        # Get unscored non-MCQ responses
        cursor.execute("""
            SELECT r.id, r.answer_text, q.question_text, q.guidelines, q.max_score, q.type
            FROM responses r
            JOIN questions q ON r.question_id = q.id
            WHERE r.candidate_id = ? AND q.type != 'MCQ'
        """, (candidate_id,))
        responses = cursor.fetchall()

        for resp in responses:
            result = await score_response(
                resp["question_text"],
                resp["guidelines"],
                resp["answer_text"] or "",
                resp["type"],
                resp["max_score"]
            )

            cursor.execute("""
                UPDATE responses
                SET score = ?, reasoning = ?, strengths = ?, gaps = ?
                WHERE id = ?
            """, (
                result["score"],
                result["reasoning"],
                json.dumps(result.get("strengths", [])),
                json.dumps(result.get("gaps", [])),
                resp["id"]
            ))

        # Recalculate total score (MCQ + AI scored)
        cursor.execute("SELECT SUM(score) FROM responses WHERE candidate_id = ?", (candidate_id,))
        total = cursor.fetchone()[0] or 0

        cursor.execute("SELECT max_score FROM candidates WHERE id = ?", (candidate_id,))
        max_s = cursor.fetchone()["max_score"]

        pct = (total / max_s * 100) if max_s > 0 else 0

        cursor.execute("""
            UPDATE candidates
            SET total_score = ?, percentage = ?, scoring_status = 'done'
            WHERE id = ?
        """, (total, pct, candidate_id))

        conn.commit()
        conn.close()
        print(f"✅ Auto-scored candidate {candidate_id}: {total}/{max_s} ({pct:.1f}%)")

    except Exception as e:
        print(f"❌ Auto-scoring failed for candidate {candidate_id}: {e}")
        try:
            conn = get_db()
            conn.execute("UPDATE candidates SET scoring_status = 'error' WHERE id = ?", (candidate_id,))
            conn.commit()
            conn.close()
        except:
            pass


def recalculate_rankings(assessment_id: int):
    """Recalculate ranks and recommendations for all candidates in an assessment"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, percentage FROM candidates
        WHERE assessment_id = ? AND has_submitted = 1
        ORDER BY percentage DESC
    """, (assessment_id,))
    candidates = cursor.fetchall()

    total = len(candidates)
    for i, c in enumerate(candidates):
        percentile = 100.0 - (i / total * 100) if total > 0 else 0

        # Top 20% → ADVANCE, Middle 50% → CONSIDER, Bottom 30% → REJECT
        if percentile >= 80:
            recommendation = "ADVANCE"
        elif percentile >= 30:
            recommendation = "CONSIDER"
        else:
            recommendation = "REJECT"

        cursor.execute(
            "UPDATE candidates SET rank = ?, percentile = ?, recommendation = ? WHERE id = ?",
            (i + 1, round(percentile, 1), recommendation, c["id"])
        )

    conn.commit()
    conn.close()


# ============================================================================
# API ROUTES - AUTH
# ============================================================================

@app.post("/api/register")
async def register(
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    company_name: str = Form("")
):
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE email = ?", (email.lower().strip(),))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = hash_password(password)
    cursor.execute(
        "INSERT INTO users (email, password, full_name, company_name) VALUES (?, ?, ?, ?)",
        (email.lower().strip(), hashed, full_name.strip(), company_name.strip())
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()

    return {"success": True, "user_id": user_id, "message": "Registration successful"}


@app.post("/api/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password, full_name FROM users WHERE email = ?", (email.lower().strip(),))
    user = cursor.fetchone()
    conn.close()

    if not user or not verify_password(password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    request.session["user_id"] = user["id"]

    return {
        "success": True,
        "user": {"id": user["id"], "email": email, "full_name": user["full_name"]}
    }


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"success": True, "message": "Logged out"}


@app.get("/api/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {"success": True, "user": user}


# ============================================================================
# API ROUTES - ASSESSMENTS
# ============================================================================

@app.post("/api/assessments")
async def create_assessment(
    request: Request,
    jd_text: str = Form(...),
    num_questions: int = Form(10),
    duration: int = Form(60)
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    if len(jd_text) < 50:
        raise HTTPException(status_code=400, detail="Job description too short (min 50 chars)")

    # Sanitize JD
    jd_clean = strip_prompt_injection(jd_text)

    # Parse JD with AI
    jd_data = await parse_job_description(jd_clean)

    # Generate questions with AI
    questions = await generate_questions(jd_data, max(5, min(20, num_questions)))

    if not questions:
        raise HTTPException(status_code=500, detail="Failed to generate questions. Check Ollama connection.")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO assessments
        (user_id, title, role_title, seniority_level, department, domain,
         experience, education, required_skills, preferred_skills, tools,
         responsibilities, soft_skills, raw_jd, duration_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user["id"],
        f"{jd_data.get('role_title', 'Untitled')} - Assessment",
        jd_data.get("role_title", "NOT SPECIFIED"),
        jd_data.get("seniority_level", "NOT SPECIFIED"),
        jd_data.get("department", "NOT SPECIFIED"),
        jd_data.get("domain", "NOT SPECIFIED"),
        jd_data.get("years_of_experience_required", "NOT SPECIFIED"),
        jd_data.get("education_requirements", "NOT SPECIFIED"),
        json.dumps(jd_data.get("required_skills", [])),
        json.dumps(jd_data.get("preferred_skills", [])),
        json.dumps(jd_data.get("tools_technologies", [])),
        json.dumps(jd_data.get("key_responsibilities", [])),
        json.dumps(jd_data.get("soft_skills", [])),
        jd_text,
        max(15, min(180, duration))
    ))

    assessment_id = cursor.lastrowid

    for i, q in enumerate(questions):
        options = q.get("options", [])
        cursor.execute("""
            INSERT INTO questions
            (assessment_id, question_number, type, skill, difficulty,
             question_text, options, correct_answer, guidelines, max_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            assessment_id,
            i + 1,
            q.get("type", "SHORT_ANSWER"),
            q.get("skill_tested", "General"),
            q.get("difficulty_level", "medium"),
            q.get("question_text", ""),
            json.dumps(options if isinstance(options, list) else []),
            q.get("correct_answer", ""),
            q.get("ideal_answer_guidelines", ""),
            q.get("max_score", 10)
        ))

    conn.commit()
    conn.close()

    return {"success": True, "assessment_id": assessment_id, "questions_created": len(questions)}


@app.get("/api/assessments")
async def list_assessments(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.*, COUNT(c.id) as candidate_count,
               COALESCE(AVG(CASE WHEN c.has_submitted = 1 THEN c.percentage END), 0) as avg_score
        FROM assessments a
        LEFT JOIN candidates c ON a.id = c.assessment_id
        WHERE a.user_id = ?
        GROUP BY a.id
        ORDER BY a.created_at DESC
    """, (user["id"],))

    assessments = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return {"success": True, "assessments": assessments}


@app.get("/api/assessments/{assessment_id}")
async def get_assessment(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    assessment = cursor.fetchone()

    if not assessment:
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    cursor.execute("SELECT * FROM questions WHERE assessment_id = ? ORDER BY question_number",
                   (assessment_id,))
    questions = [dict(row) for row in cursor.fetchall()]

    conn.close()

    result = dict(assessment)
    result["questions"] = questions

    return {"success": True, "assessment": result}


@app.post("/api/assessments/{assessment_id}/publish")
async def publish_assessment(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    # Check has questions
    cursor.execute("SELECT COUNT(*) FROM questions WHERE assessment_id = ?", (assessment_id,))
    if cursor.fetchone()[0] == 0:
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot publish assessment with no questions")

    cursor.execute(
        "UPDATE assessments SET status = 'active', published_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (assessment_id, user["id"])
    )
    conn.commit()
    conn.close()

    return {"success": True, "message": "Assessment published"}


@app.post("/api/assessments/{assessment_id}/close")
async def close_assessment(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE assessments SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (assessment_id, user["id"])
    )
    conn.commit()
    conn.close()

    return {"success": True, "message": "Assessment closed"}


@app.delete("/api/assessments/{assessment_id}")
async def delete_assessment(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()
    # Verify ownership
    cursor.execute("SELECT id FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    cursor.execute("DELETE FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    conn.commit()
    conn.close()

    return {"success": True, "message": "Assessment deleted"}


@app.patch("/api/assessments/{assessment_id}")
async def update_assessment(assessment_id: int, request: Request):
    """Edit assessment title and/or duration"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    data = await request.json()
    title = data.get("title", "").strip()
    duration = data.get("duration_minutes")

    if not title and duration is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    updates = []
    params = []
    if title:
        updates.append("title = ?")
        params.append(title)
    if duration is not None:
        updates.append("duration_minutes = ?")
        params.append(int(duration))

    params.append(assessment_id)
    cursor.execute(f"UPDATE assessments SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()

    return {"success": True, "message": "Assessment updated"}


# ============================================================================
# API ROUTES - CANDIDATES (PUBLIC)
# ============================================================================

@app.get("/api/public/assessments/{assessment_id}")
async def get_public_assessment(assessment_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, role_title, department, duration_minutes FROM assessments WHERE id = ? AND status = 'active'",
        (assessment_id,)
    )
    assessment = cursor.fetchone()

    if not assessment:
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found or not active")

    cursor.execute("SELECT COUNT(*) as count FROM questions WHERE assessment_id = ?", (assessment_id,))
    count = cursor.fetchone()["count"]

    conn.close()

    result = dict(assessment)
    result["question_count"] = count

    return {"success": True, "assessment": result}


@app.post("/api/candidates/register")
async def register_candidate(
    assessment_id: int = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form("")
):
    email = email.lower().strip()
    conn = get_db()
    cursor = conn.cursor()

    # Check if assessment is active
    cursor.execute("SELECT status FROM assessments WHERE id = ?", (assessment_id,))
    assessment = cursor.fetchone()

    if not assessment or assessment["status"] != "active":
        conn.close()
        raise HTTPException(status_code=400, detail="Assessment not available")

    # Check if already submitted
    cursor.execute(
        "SELECT id, has_submitted FROM candidates WHERE assessment_id = ? AND email = ?",
        (assessment_id, email)
    )
    existing = cursor.fetchone()

    if existing:
        if existing["has_submitted"]:
            conn.close()
            raise HTTPException(status_code=400, detail="You have already submitted this assessment")
        # Already registered but not submitted - return existing ID
        conn.close()
        return {"success": True, "candidate_id": existing["id"], "message": "Already registered"}

    # Create candidate
    cursor.execute(
        "INSERT INTO candidates (assessment_id, full_name, email, phone) VALUES (?, ?, ?, ?)",
        (assessment_id, full_name.strip(), email, phone.strip())
    )
    conn.commit()
    candidate_id = cursor.lastrowid
    conn.close()

    return {"success": True, "candidate_id": candidate_id}


@app.get("/api/candidates/{candidate_id}/test")
async def get_test(candidate_id: int):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.*, a.title, a.duration_minutes
        FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE c.id = ?
    """, (candidate_id,))
    candidate = cursor.fetchone()

    if not candidate:
        conn.close()
        raise HTTPException(status_code=404, detail="Candidate not found")

    if candidate["has_submitted"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Test already submitted")

    # Check assessment still active
    cursor.execute("SELECT status FROM assessments WHERE id = ?", (candidate["assessment_id"],))
    assessment = cursor.fetchone()
    if not assessment or assessment["status"] != "active":
        conn.close()
        raise HTTPException(status_code=400, detail="Assessment is no longer active")

    # Update started time only if not already started
    if not candidate["started_at"]:
        cursor.execute(
            "UPDATE candidates SET started_at = CURRENT_TIMESTAMP WHERE id = ?",
            (candidate_id,)
        )
        conn.commit()

    # Get questions (without answers/guidelines)
    cursor.execute("""
        SELECT id, question_number, type, skill, difficulty, question_text, options, max_score
        FROM questions
        WHERE assessment_id = ?
        ORDER BY question_number
    """, (candidate["assessment_id"],))
    questions = []
    for row in cursor.fetchall():
        q = dict(row)
        # Parse options safely
        try:
            q["options"] = json.loads(q["options"]) if q["options"] else []
        except:
            q["options"] = []
        questions.append(q)

    conn.close()

    return {
        "success": True,
        "candidate_id": candidate_id,
        "assessment_title": candidate["title"],
        "duration_minutes": candidate["duration_minutes"],
        "questions": questions
    }


@app.post("/api/candidates/{candidate_id}/submit")
async def submit_test(candidate_id: int, request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    answers = data.get("answers", [])
    time_spent = data.get("time_spent_minutes", 0)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    candidate = cursor.fetchone()

    if not candidate or candidate["has_submitted"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Test already submitted or not found")

    assessment_id = candidate["assessment_id"]

    cursor.execute("SELECT * FROM questions WHERE assessment_id = ?", (assessment_id,))
    questions = {row["id"]: dict(row) for row in cursor.fetchall()}

    total_score = 0.0
    max_total = 0.0

    for answer in answers:
        question_id = answer.get("question_id")
        question = questions.get(question_id)

        if not question:
            continue

        # Sanitize answer input
        answer_text = strip_prompt_injection(answer.get("answer_text", "") or "")
        selected_option = (answer.get("selected_option", "") or "").upper().strip()

        max_total += question["max_score"]

        # Auto-score MCQ immediately
        score = 0.0
        reasoning = ""
        if question["type"] == "MCQ":
            correct = (question.get("correct_answer") or "").upper().strip()
            if selected_option and correct and selected_option == correct:
                score = float(question["max_score"])
                reasoning = "Correct answer selected"
            elif selected_option:
                reasoning = f"Incorrect. Correct answer: {correct}"
            else:
                reasoning = "No answer selected"

        cursor.execute("""
            INSERT INTO responses
            (candidate_id, question_id, answer_text, selected_option, score, reasoning)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            candidate_id,
            question_id,
            answer_text,
            selected_option,
            score,
            reasoning
        ))

        total_score += score

    percentage = (total_score / max_total * 100) if max_total > 0 else 0
    cursor.execute("""
        UPDATE candidates
        SET has_submitted = 1, submitted_at = CURRENT_TIMESTAMP,
            time_spent = ?, total_score = ?, max_score = ?, percentage = ?,
            scoring_status = 'pending'
        WHERE id = ?
    """, (time_spent, total_score, max_total, percentage, candidate_id))

    conn.commit()
    conn.close()

    # Trigger AI scoring in background for non-MCQ questions
    background_tasks.add_task(auto_score_candidate, candidate_id)

    return {"success": True, "message": "Test submitted successfully. AI scoring in progress."}


@app.get("/api/candidates/{candidate_id}/status")
async def get_candidate_status(candidate_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    candidate = cursor.fetchone()
    conn.close()

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    return {
        "success": True,
        "has_submitted": bool(candidate["has_submitted"]),
        "scoring_status": candidate["scoring_status"],
        "total_score": candidate["total_score"],
        "max_score": candidate["max_score"],
        "percentage": candidate["percentage"]
    }


# ============================================================================
# API ROUTES - SCORING (MANUAL / HR TRIGGERED)
# ============================================================================

@app.post("/api/score/{candidate_id}")
async def score_candidate(candidate_id: int, request: Request, background_tasks: BackgroundTasks):
    """Manually trigger AI scoring for a candidate"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    # Rate limiting
    now = time.time()
    last_scored = _score_rate_limit.get(candidate_id, 0)
    if now - last_scored < SCORE_RATE_LIMIT_SECONDS:
        raise HTTPException(status_code=429, detail="Please wait before scoring again")
    _score_rate_limit[candidate_id] = now

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.* FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE c.id = ? AND a.user_id = ?
    """, (candidate_id, user["id"]))
    candidate = cursor.fetchone()
    conn.close()

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    background_tasks.add_task(auto_score_candidate, candidate_id)

    return {"success": True, "message": "Scoring started in background"}


# ============================================================================
# API ROUTES - RESULTS
# ============================================================================

@app.get("/api/assessments/{assessment_id}/results")
async def get_results(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    cursor.execute("""
        SELECT * FROM candidates
        WHERE assessment_id = ? AND has_submitted = 1
        ORDER BY percentage DESC
    """, (assessment_id,))
    candidates = [dict(row) for row in cursor.fetchall()]

    # Recalculate rankings
    total = len(candidates)
    for i, c in enumerate(candidates):
        percentile = round(100.0 - (i / total * 100), 1) if total > 0 else 0

        if percentile >= 80:
            recommendation = "ADVANCE"
        elif percentile >= 30:
            recommendation = "CONSIDER"
        else:
            recommendation = "REJECT"

        cursor.execute(
            "UPDATE candidates SET rank = ?, percentile = ?, recommendation = ? WHERE id = ?",
            (i + 1, percentile, recommendation, c["id"])
        )

        candidates[i]["rank"] = i + 1
        candidates[i]["percentile"] = percentile
        candidates[i]["recommendation"] = recommendation

    conn.commit()
    conn.close()

    return {"success": True, "candidates": candidates}


@app.get("/api/assessments/{assessment_id}/leaderboard")
async def get_leaderboard(assessment_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, title, role_title FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    assessment = cursor.fetchone()

    if not assessment:
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    cursor.execute("""
        SELECT full_name, email, phone, total_score, max_score, percentage,
               rank, percentile, recommendation, scoring_status, submitted_at
        FROM candidates
        WHERE assessment_id = ? AND has_submitted = 1
        ORDER BY rank
    """, (assessment_id,))
    entries = [dict(row) for row in cursor.fetchall()]

    advance = sum(1 for e in entries if e["recommendation"] == "ADVANCE")
    consider = sum(1 for e in entries if e["recommendation"] == "CONSIDER")
    reject = sum(1 for e in entries if e["recommendation"] == "REJECT")
    avg = sum(e["percentage"] for e in entries) / len(entries) if entries else 0

    conn.close()

    return {
        "success": True,
        "assessment_title": assessment["title"],
        "role_title": assessment["role_title"],
        "total_candidates": len(entries),
        "average_score": round(avg, 1),
        "advance_count": advance,
        "consider_count": consider,
        "reject_count": reject,
        "entries": entries
    }


@app.get("/api/assessments/{assessment_id}/results/export")
async def export_results_csv(assessment_id: int, request: Request):
    """Export results as CSV"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT title FROM assessments WHERE id = ? AND user_id = ?",
                   (assessment_id, user["id"]))
    assessment = cursor.fetchone()
    if not assessment:
        conn.close()
        raise HTTPException(status_code=404, detail="Assessment not found")

    cursor.execute("""
        SELECT full_name, email, phone, total_score, max_score, percentage,
               rank, percentile, recommendation, scoring_status,
               submitted_at, time_spent
        FROM candidates
        WHERE assessment_id = ? AND has_submitted = 1
        ORDER BY rank
    """, (assessment_id,))
    candidates = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Rank", "Name", "Email", "Phone",
        "Score", "Max Score", "Percentage (%)",
        "Percentile", "Recommendation", "Scoring Status",
        "Submitted At", "Time Spent (min)"
    ])

    for c in candidates:
        writer.writerow([
            c["rank"] or "",
            c["full_name"],
            c["email"],
            c["phone"] or "",
            round(c["total_score"], 1) if c["total_score"] else 0,
            round(c["max_score"], 1) if c["max_score"] else 0,
            round(c["percentage"], 1) if c["percentage"] else 0,
            round(c["percentile"], 1) if c["percentile"] else "",
            c["recommendation"] or "PENDING",
            c["scoring_status"] or "pending",
            c["submitted_at"] or "",
            c["time_spent"] or ""
        ])

    csv_content = output.getvalue()
    filename = assessment["title"].replace(" ", "_").replace("/", "-")

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}_results.csv"
        }
    )


@app.get("/api/candidates/{candidate_id}/details")
async def get_candidate_details(candidate_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.*, a.title as assessment_title, a.role_title, a.department, a.id as assessment_id
        FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE c.id = ? AND a.user_id = ?
    """, (candidate_id, user["id"]))
    candidate = cursor.fetchone()

    if not candidate:
        conn.close()
        raise HTTPException(status_code=404, detail="Candidate not found")

    cursor.execute("""
        SELECT r.id, r.answer_text, r.selected_option, r.score, r.reasoning, r.strengths, r.gaps,
               q.question_number, q.type, q.skill, q.question_text, q.max_score,
               q.options as question_options, q.correct_answer
        FROM responses r
        JOIN questions q ON r.question_id = q.id
        WHERE r.candidate_id = ?
        ORDER BY q.question_number
    """, (candidate_id,))
    responses = []
    for row in cursor.fetchall():
        r = dict(row)
        try:
            r["strengths"] = json.loads(r["strengths"]) if r["strengths"] else []
        except:
            r["strengths"] = []
        try:
            r["gaps"] = json.loads(r["gaps"]) if r["gaps"] else []
        except:
            r["gaps"] = []
        try:
            r["question_options"] = json.loads(r["question_options"]) if r["question_options"] else []
        except:
            r["question_options"] = []
        responses.append(r)

    conn.close()

    result = dict(candidate)
    result["responses"] = responses

    return {"success": True, "candidate": result}


@app.patch("/api/candidates/{candidate_id}/notes")
async def save_candidate_notes(candidate_id: int, request: Request):
    """Save private HR notes on a candidate"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    data = await request.json()
    notes = data.get("notes", "")

    conn = get_db()
    cursor = conn.cursor()
    # Verify this candidate belongs to an assessment owned by the HR user
    cursor.execute("""
        SELECT c.id FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE c.id = ? AND a.user_id = ?
    """, (candidate_id, user["id"]))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Candidate not found")

    cursor.execute("UPDATE candidates SET notes = ? WHERE id = ?", (notes, candidate_id))
    conn.commit()
    conn.close()

    return {"success": True, "message": "Notes saved"}


@app.get("/api/dashboard")
async def get_dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM assessments WHERE user_id = ?", (user["id"],))
    total_assessments = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM assessments WHERE user_id = ? AND status = 'active'",
                   (user["id"],))
    active = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM assessments WHERE user_id = ? AND status = 'closed'",
                   (user["id"],))
    closed = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE a.user_id = ? AND c.has_submitted = 1
    """, (user["id"],))
    total_candidates = cursor.fetchone()[0]

    cursor.execute("""
        SELECT AVG(c.percentage) FROM candidates c
        JOIN assessments a ON c.assessment_id = a.id
        WHERE a.user_id = ? AND c.has_submitted = 1 AND c.scoring_status = 'done'
    """, (user["id"],))
    avg = cursor.fetchone()[0] or 0

    conn.close()

    return {
        "success": True,
        "stats": {
            "total_assessments": total_assessments,
            "active_assessments": active,
            "closed_assessments": closed,
            "total_candidates": total_candidates,
            "average_score": round(avg, 1)
        }
    }



# ============================================================================
# INTERNAL API (called by cloud backend via Cloudflare tunnel)
# ============================================================================

def verify_internal_key(request: Request):
    """Verify the internal API key header from cloud backend"""
    key = request.headers.get("X-Internal-Key", "")
    if not INTERNAL_API_KEY or key != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal API key")


@app.post("/generate-exam")
async def generate_exam_endpoint(request: Request):
    """Generate MCQ exam from job description — called by cloud backend"""
    verify_internal_key(request)
    data = await request.json()
    jd_text = data.get("job_description", "")
    num = data.get("num_questions", 10)

    if len(jd_text) < 30:
        raise HTTPException(status_code=400, detail="Job description too short")

    # Reuse existing AI pipeline
    jd_data = await parse_job_description(strip_prompt_injection(jd_text))
    questions = await generate_questions(jd_data, max(5, min(20, num)))

    if not questions:
        raise HTTPException(status_code=500, detail="Failed to generate questions. Is Ollama running?")

    # Normalize to cloud-expected format
    result = []
    for i, q in enumerate(questions):
        result.append({
            "id": i + 1,
            "question": q.get("question_text", ""),
            "options": q.get("options", []),
            "correct_answer": q.get("correct_answer", ""),
            "type": q.get("type", "MCQ"),
            "skill": q.get("skill_tested", "General"),
            "difficulty": q.get("difficulty_level", "medium"),
            "guidelines": q.get("ideal_answer_guidelines", ""),
            "max_score": q.get("max_score", 10),
        })

    return {"questions": result, "jd_parsed": jd_data}


@app.post("/grade-open-ended")
async def grade_open_ended_endpoint(request: Request):
    """Grade an open-ended answer using local LLM — called by cloud backend"""
    verify_internal_key(request)
    data = await request.json()

    result = await score_response(
        question=data.get("question", ""),
        guidelines=data.get("ideal_hints", ""),
        answer=data.get("candidate_answer", ""),
        q_type="SHORT_ANSWER",
        max_score=10,
    )

    return {"score": result["score"], "feedback": result["reasoning"]}


# ============================================================================
# STATIC FILE SERVING
# ============================================================================

# Resolve frontend directory relative to this file (works regardless of cwd)
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_FRONTEND_DIR = os.path.normpath(os.path.join(_BACKEND_DIR, "..", "frontend"))

@app.get("/")
async def root_redirect():
    """Redirect root to login page"""
    return RedirectResponse(url="/pages/login.html")

# Mount frontend static files — MUST come after all /api routes
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/pages", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "pages"), html=True), name="pages")
    app.mount("/css", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "css")), name="css")
    app.mount("/js", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "js")), name="js")
    # Also mount assets if present
    assets_dir = os.path.join(_FRONTEND_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
else:
    print(f"⚠️  Frontend directory not found at: {_FRONTEND_DIR}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)