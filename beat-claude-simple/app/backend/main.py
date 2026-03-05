"""
Beat Claude — Unified Backend
Single FastAPI server: JWT auth, Groq API for AI, SQLite database.
Serves both recruiter dashboard and candidate exam pages.
"""
import os
import re
import json
import secrets
import hashlib
import sqlite3
import httpx
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
JWT_SECRET   = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_EXPIRY_H = 24
DB_PATH      = os.getenv("DB_PATH", "beat_claude.db")
APP_URL      = os.getenv("APP_URL", "http://localhost:8000")   # public URL for exam links

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        email        TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS exams (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        slug             TEXT UNIQUE NOT NULL,
        title            TEXT NOT NULL,
        role_title       TEXT DEFAULT '',
        questions_json   TEXT NOT NULL,
        duration_minutes INTEGER DEFAULT 60,
        num_questions    INTEGER DEFAULT 10,
        recruiter_email  TEXT NOT NULL,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_exams_slug    ON exams(slug)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_exams_email   ON exams(recruiter_email)")

    c.execute("""CREATE TABLE IF NOT EXISTS candidates (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_slug      TEXT NOT NULL,
        name           TEXT NOT NULL,
        email          TEXT NOT NULL,
        phone          TEXT DEFAULT '',
        started_at     TIMESTAMP,
        submitted_at   TIMESTAMP,
        tab_violations INTEGER DEFAULT 0,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (exam_slug) REFERENCES exams(slug),
        UNIQUE(exam_slug, email)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cands_slug ON candidates(exam_slug)")

    c.execute("""CREATE TABLE IF NOT EXISTS answers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        question_id  INTEGER NOT NULL,
        selected_opt TEXT DEFAULT '',
        is_correct   INTEGER DEFAULT 0,
        ai_score     REAL DEFAULT -1,
        ai_feedback  TEXT DEFAULT '',
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_answers_cand ON answers(candidate_id)")

    conn.commit()
    conn.close()
    print("✅ Database ready")


# ─── App Setup ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Beat Claude", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Auth Helpers ─────────────────────────────────────────────────────────────

def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())

def make_jwt(email: str) -> str:
    payload = {
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_H),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def require_auth(request: Request) -> str:
    """Extract JWT from Authorization header and return email."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload["email"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token. Please sign in again.")


# ─── Groq AI Helpers ─────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 10))
async def call_groq(prompt: str, system: str = "") -> str:
    """Call Groq chat completions API."""
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured on server.")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "temperature": 0.2,
                "top_p": 0.8,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def strip_injections(text: str) -> str:
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
    for p in patterns:
        text = re.sub(p, '[REMOVED]', text, flags=re.IGNORECASE)
    return text[:10000]


async def parse_jd(jd_text: str) -> dict:
    system = ("You are an expert HR assistant. Extract structured information from job descriptions. "
              "Return ONLY valid JSON. No commentary.")
    prompt = f"""Parse this job description:

JOB DESCRIPTION:
{jd_text}

Return ONLY this JSON (no markdown):
{{
    "role_title": "Job title",
    "seniority_level": "entry/junior/mid/senior/lead",
    "department": "Department name",
    "domain": "Industry domain",
    "years_of_experience_required": "Experience range",
    "education_requirements": "Education level",
    "required_skills": ["skill1", "skill2"],
    "preferred_skills": ["skill1"],
    "tools_technologies": ["tool1"],
    "key_responsibilities": ["resp1"],
    "soft_skills": ["skill1"]
}}
Use "NOT SPECIFIED" for missing strings and [] for missing arrays."""
    try:
        resp = await call_groq(prompt, system)
        s, e = resp.find('{'), resp.rfind('}') + 1
        return json.loads(resp[s:e]) if s >= 0 and e > s else json.loads(resp)
    except Exception as ex:
        print(f"JD parse error: {ex}")
        return {"role_title": "Unknown", "seniority_level": "mid", "department": "NOT SPECIFIED",
                "domain": "NOT SPECIFIED", "years_of_experience_required": "NOT SPECIFIED",
                "education_requirements": "NOT SPECIFIED", "required_skills": [],
                "preferred_skills": [], "tools_technologies": [],
                "key_responsibilities": [], "soft_skills": []}


async def generate_questions(jd: dict, num: int = 10) -> list:
    system = ("You are an expert technical interviewer. Create high-quality interview questions. "
              "Return ONLY valid JSON array. No markdown.")
    skills = ", ".join(jd.get("required_skills", [])[:6])
    seniority = jd.get("seniority_level", "mid").lower()
    if seniority in ["senior", "lead", "principal"]:
        ratio = "20% MCQ, 30% SHORT_ANSWER, 50% SCENARIO"
    elif seniority in ["junior", "entry"]:
        ratio = "50% MCQ, 30% SHORT_ANSWER, 20% SCENARIO"
    else:
        ratio = "30% MCQ, 30% SHORT_ANSWER, 40% SCENARIO"

    prompt = f"""Generate exactly {num} interview questions for this role.

ROLE: {jd.get('role_title', 'Unknown')}
SENIORITY: {seniority}
REQUIRED SKILLS: {skills}
RESPONSIBILITIES: {', '.join(jd.get('key_responsibilities', [])[:3])}

Create a mix: {ratio}

For MCQ: options = ["Option A", "Option B", "Option C", "Option D"], correct_answer = "A"|"B"|"C"|"D"
For SHORT_ANSWER/SCENARIO: options = [], correct_answer = ""

Return ONLY this JSON array:
[
  {{
    "id": 1,
    "type": "MCQ",
    "skill": "specific skill",
    "difficulty": "easy",
    "question": "The question text?",
    "options": ["First option", "Second option", "Third option", "Fourth option"],
    "correct_answer": "A",
    "guidelines": "Why A is correct and what to look for",
    "max_score": 10
  }},
  {{
    "id": 2,
    "type": "SHORT_ANSWER",
    "skill": "specific skill",
    "difficulty": "medium",
    "question": "The question text?",
    "options": [],
    "correct_answer": "",
    "guidelines": "Key points the ideal answer should cover",
    "max_score": 10
  }}
]"""
    try:
        resp = await call_groq(prompt, system)
        s, e = resp.find('['), resp.rfind(']') + 1
        qs = json.loads(resp[s:e]) if s >= 0 and e > s else json.loads(resp)
        clean = []
        for i, q in enumerate(qs):
            if not q.get("question"):
                continue
            q["id"] = i + 1
            q["type"] = q.get("type", "SHORT_ANSWER").upper()
            if q["type"] not in ["MCQ", "SHORT_ANSWER", "SCENARIO"]:
                q["type"] = "SHORT_ANSWER"
            if q["type"] != "MCQ":
                q["options"] = []
                q["correct_answer"] = ""
            q["max_score"] = int(q.get("max_score", 10))
            clean.append(q)
        return clean
    except Exception as ex:
        print(f"Question gen error: {ex}")
        return []


async def score_answer(question: str, guidelines: str, answer: str, max_score: int) -> dict:
    system = ("You are an expert technical interviewer. Score responses objectively. "
              "Return ONLY valid JSON.")
    answer = strip_injections(answer or "")
    prompt = f"""Score this candidate response.

QUESTION: {question}
IDEAL ANSWER GUIDELINES: {guidelines}
CANDIDATE'S ANSWER: {answer if answer else "(no answer provided)"}
MAXIMUM SCORE: {max_score}

Be strict. No points for empty or irrelevant answers.

Return ONLY this JSON:
{{
    "score": <number 0 to {max_score}>,
    "reasoning": "Brief explanation",
    "feedback": "Constructive feedback for the candidate"
}}"""
    for attempt in range(3):
        try:
            resp = await call_groq(prompt, system)
            s, e = resp.find('{'), resp.rfind('}') + 1
            result = json.loads(resp[s:e] if s >= 0 and e > s else resp)
            score = float(result.get("score", 0))
            return {
                "score": max(0.0, min(float(max_score), score)),
                "feedback": result.get("feedback", result.get("reasoning", "")),
            }
        except Exception as ex:
            print(f"Score attempt {attempt+1} failed: {ex}")
    return {"score": 0, "feedback": "Could not evaluate response."}


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.post("/auth/signup")
async def signup(request: Request):
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email is required")
    if not password or len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email,))
    if c.fetchone():
        conn.close()
        raise HTTPException(400, "An account with this email already exists")
    c.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, hash_pw(password)))
    conn.commit()
    conn.close()
    return {"success": True, "token": make_jwt(email), "email": email}


@app.post("/auth/signin")
async def signin(request: Request):
    data = await request.json()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        raise HTTPException(400, "Email and password are required")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT password_hash FROM users WHERE email = ?", (email,))
    row = c.fetchone()
    conn.close()
    if not row or not verify_pw(password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    return {"success": True, "token": make_jwt(email), "email": email}


@app.get("/auth/me")
async def auth_me(request: Request):
    email = require_auth(request)
    return {"success": True, "email": email}


# ─── Exam Creation ────────────────────────────────────────────────────────────

@app.post("/api/create-exam")
async def create_exam(request: Request):
    """Recruiter creates an exam — calls Groq directly to generate questions."""
    email = require_auth(request)
    data = await request.json()
    jd_text       = (data.get("job_description") or "").strip()
    num_questions = min(20, max(5, int(data.get("num_questions", 10))))
    duration      = min(180, max(15, int(data.get("duration_minutes", 60))))

    if len(jd_text) < 30:
        raise HTTPException(400, "Job description is too short (min 30 chars)")

    jd_clean = strip_injections(jd_text)
    jd_data  = await parse_jd(jd_clean)
    questions = await generate_questions(jd_data, num_questions)

    if not questions:
        raise HTTPException(500, "Failed to generate questions. Check your GROQ_API_KEY.")

    slug = secrets.token_urlsafe(9)
    conn = get_db()
    c = conn.cursor()
    while True:
        c.execute("SELECT id FROM exams WHERE slug = ?", (slug,))
        if not c.fetchone():
            break
        slug = secrets.token_urlsafe(9)

    title = f"{jd_data.get('role_title', 'Untitled')} — Assessment"
    c.execute("""
        INSERT INTO exams (slug, title, role_title, questions_json, duration_minutes, num_questions, recruiter_email)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (slug, title, jd_data.get("role_title", ""), json.dumps(questions),
          duration, len(questions), email))
    conn.commit()
    conn.close()

    exam_link = f"{APP_URL.rstrip('/')}/exam/{slug}"
    return {"success": True, "slug": slug, "exam_link": exam_link,
            "title": title, "num_questions": len(questions)}


# ─── Recruiter Dashboard API ──────────────────────────────────────────────────

@app.get("/recruiter/exams")
async def recruiter_exams(email: str = ""):
    """Return all exams for a recruiter email with candidate stats."""
    if not email:
        raise HTTPException(400, "email parameter required")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM exams WHERE LOWER(recruiter_email) = LOWER(?) ORDER BY created_at DESC",
              (email.strip(),))
    exams = [dict(r) for r in c.fetchall()]

    result = []
    for ex in exams:
        slug = ex["slug"]
        c.execute("SELECT COUNT(*) as cnt FROM candidates WHERE exam_slug = ? AND submitted_at IS NOT NULL", (slug,))
        cc = c.fetchone()["cnt"]
        avg = 0
        if cc > 0:
            qs = json.loads(ex["questions_json"])
            total_q = len(qs)
            c.execute("SELECT id FROM candidates WHERE exam_slug = ? AND submitted_at IS NOT NULL", (slug,))
            cids = [r["id"] for r in c.fetchall()]
            scores = []
            for cid in cids:
                c.execute("SELECT SUM(is_correct) as correct FROM answers WHERE candidate_id = ?", (cid,))
                row = c.fetchone()
                correct = row["correct"] or 0
                scores.append(round(correct / total_q * 100) if total_q > 0 else 0)
            avg = round(sum(scores) / len(scores)) if scores else 0
        result.append({
            "slug": slug, "title": ex["title"], "role_title": ex["role_title"],
            "num_questions": ex["num_questions"], "duration_minutes": ex["duration_minutes"],
            "candidate_count": cc, "avg_score": avg, "created_at": ex["created_at"],
            "exam_link": f"{APP_URL.rstrip('/')}/exam/{slug}",
        })
    conn.close()
    return {"success": True, "exams": result}


@app.get("/recruiter/results/{slug}")
async def recruiter_results(slug: str, request: Request):
    """Return full results for an exam."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = c.fetchone()
    if not exam:
        conn.close()
        raise HTTPException(404, "Exam not found")

    questions = json.loads(exam["questions_json"])
    q_map = {q["id"]: q for q in questions}

    c.execute("SELECT * FROM candidates WHERE exam_slug = ? ORDER BY submitted_at DESC", (slug,))
    candidates = [dict(r) for r in c.fetchall()]

    for cand in candidates:
        c.execute("SELECT * FROM answers WHERE candidate_id = ? ORDER BY question_id", (cand["id"],))
        raw = [dict(r) for r in c.fetchall()]
        enriched = []
        for a in raw:
            q = q_map.get(a["question_id"], {})
            enriched.append({
                "question_id": a["question_id"],
                "question_text": q.get("question", ""),
                "question_type": q.get("type", "MCQ"),
                "options": q.get("options", []),
                "correct_answer": q.get("correct_answer", ""),
                "skill": q.get("skill", ""),
                "difficulty": q.get("difficulty", "medium"),
                "max_score": q.get("max_score", 10),
                "candidate_answer": a["selected_opt"],
                "is_correct": a["is_correct"],
                "ai_score": a["ai_score"],
                "ai_feedback": a["ai_feedback"],
            })
        cand["answers"] = enriched
        mcq_correct = sum(1 for a in enriched if a["is_correct"])
        mcq_total   = sum(1 for q in questions if q.get("type", "MCQ") == "MCQ")
        ai_scored   = [a for a in enriched if a["ai_score"] >= 0]
        ai_avg      = sum(a["ai_score"] for a in ai_scored) / len(ai_scored) if ai_scored else 0
        cand["mcq_score"]      = f"{mcq_correct}/{mcq_total}" if mcq_total > 0 else "N/A"
        cand["mcq_correct"]    = mcq_correct
        cand["mcq_total"]      = mcq_total
        cand["ai_average"]     = round(ai_avg, 1)
        cand["total_questions"] = len(questions)
    conn.close()

    return {
        "success": True,
        "exam": {
            "slug": exam["slug"], "title": exam["title"], "role_title": exam["role_title"],
            "num_questions": exam["num_questions"], "duration_minutes": exam["duration_minutes"],
            "recruiter_email": exam["recruiter_email"], "created_at": exam["created_at"],
        },
        "questions": questions,
        "candidates": candidates,
    }


# ─── Candidate Exam Routes ────────────────────────────────────────────────────

@app.get("/exam/{slug}", response_class=HTMLResponse)
async def exam_page(slug: str):
    """Serve the candidate exam page (HTML)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = c.fetchone()
    conn.close()
    if not exam:
        return HTMLResponse(_error_html("Exam Not Found", "This exam link is invalid or has expired."), 404)

    questions = json.loads(exam["questions_json"])
    safe_qs = [
        {"id": q["id"], "question": q["question"], "options": q.get("options", []),
         "type": q.get("type", "MCQ"), "skill": q.get("skill", ""),
         "difficulty": q.get("difficulty", "medium"), "max_score": q.get("max_score", 10)}
        for q in questions
    ]
    return HTMLResponse(_exam_html(
        slug=slug, title=exam["title"], role_title=exam["role_title"],
        duration=exam["duration_minutes"], questions_json=json.dumps(safe_qs),
        num_questions=exam["num_questions"],
    ))


@app.post("/exam/{slug}/submit")
async def submit_exam(slug: str, request: Request, background_tasks: BackgroundTasks):
    """Submit candidate answers; auto-grade MCQs; score open-ended in background."""
    data = await request.json()
    name           = (data.get("name") or "").strip()
    cand_email     = (data.get("email") or "").strip().lower()
    phone          = (data.get("phone") or "").strip()
    answers_map    = data.get("answers", {})
    tab_violations = int(data.get("tab_violations", 0))

    if not name or not cand_email:
        raise HTTPException(400, "Name and email are required")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = c.fetchone()
    if not exam:
        conn.close()
        raise HTTPException(404, "Exam not found")

    # Check duplicate submission
    c.execute("SELECT id, submitted_at FROM candidates WHERE exam_slug = ? AND email = ?",
              (slug, cand_email))
    existing = c.fetchone()
    if existing and existing["submitted_at"]:
        conn.close()
        raise HTTPException(400, "You have already submitted this exam")

    # Upsert candidate
    if existing:
        candidate_id = existing["id"]
        c.execute("UPDATE candidates SET submitted_at = CURRENT_TIMESTAMP, tab_violations = ? WHERE id = ?",
                  (tab_violations, candidate_id))
    else:
        c.execute("""INSERT INTO candidates (exam_slug, name, email, phone, started_at, submitted_at, tab_violations)
                     VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)""",
                  (slug, name, cand_email, phone, tab_violations))
        candidate_id = c.lastrowid

    questions = json.loads(exam["questions_json"])
    q_map = {str(q["id"]): q for q in questions}
    mcq_correct, mcq_total, open_ended = 0, 0, []

    for qid_str, selected in answers_map.items():
        q = q_map.get(qid_str)
        if not q:
            continue
        is_correct, ai_score, ai_fb = 0, -1.0, ""
        if q.get("type", "MCQ") == "MCQ":
            mcq_total += 1
            if ((q.get("correct_answer") or "").upper().strip() ==
                    (selected or "").upper().strip()):
                is_correct = 1
                mcq_correct += 1
        else:
            open_ended.append({
                "q_id": qid_str, "question": q.get("question", ""),
                "guidelines": q.get("guidelines", ""),
                "answer": selected or "",
                "max_score": q.get("max_score", 10),
            })
        c.execute("""INSERT INTO answers (candidate_id, question_id, selected_opt, is_correct, ai_score, ai_feedback)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (candidate_id, int(qid_str), selected or "", is_correct, ai_score, ai_fb))

    conn.commit()
    conn.close()

    # Score open-ended answers in the background
    if open_ended:
        background_tasks.add_task(_score_open_ended, candidate_id, open_ended)

    return {"status": "submitted",
            "mcq_score": f"{mcq_correct}/{mcq_total}" if mcq_total > 0 else "N/A",
            "candidate_id": candidate_id}


async def _score_open_ended(candidate_id: int, items: list):
    """Background task: score open-ended answers via Groq."""
    conn = get_db()
    c = conn.cursor()
    for item in items:
        try:
            result = await score_answer(item["question"], item["guidelines"],
                                        item["answer"], item["max_score"])
            c.execute("UPDATE answers SET ai_score = ?, ai_feedback = ? WHERE candidate_id = ? AND question_id = ?",
                      (result["score"], result["feedback"], candidate_id, int(item["q_id"])))
        except Exception as ex:
            print(f"⚠️  Open-ended scoring failed for Q{item['q_id']}: {ex}")
    conn.commit()
    conn.close()
    print(f"✅ Scored open-ended answers for candidate {candidate_id}")


# ─── Static Frontend ─────────────────────────────────────────────────────────

import pathlib
FRONTEND_DIR = pathlib.Path(__file__).parent / "frontend"

@app.get("/")
async def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Beat Claude API", "docs": "/docs"}

# Mount static frontend (HTML, CSS, JS) at the root (/static won't work nice so use custom handler)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/{filename:path}")
async def serve_frontend(filename: str):
    """Serve any frontend file not caught by other routes."""
    file_path = FRONTEND_DIR / filename
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    # Fallback to index.html for SPA-style routing
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(404, f"Not found: {filename}")


# ─── HTML Templates ──────────────────────────────────────────────────────────

def _error_html(title: str, msg: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title} — Beat Claude</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',sans-serif;background:#0a0a0b;color:#f4f4f5;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#111113;border:1px solid #27272a;border-radius:1.25rem;padding:3rem;max-width:480px;text-align:center}}
h1{{font-size:1.75rem;font-weight:800;color:#7c3aed;margin-bottom:1rem}}
p{{color:#a1a1aa;line-height:1.6}}
a{{color:#7c3aed;font-weight:600;margin-top:1.5rem;display:block}}
</style></head><body>
<div class="card"><h1>{title}</h1><p>{msg}</p><a href="/">← Back to home</a></div>
</body></html>"""


def _exam_html(slug: str, title: str, role_title: str, duration: int,
               questions_json: str, num_questions: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title} — Beat Claude Exam</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --bg:#0a0a0b;--surface:#111113;--surface2:#18181b;--surface3:#1f1f23;
  --border:#27272a;--border2:#3f3f46;--text:#f4f4f5;--text2:#a1a1aa;--text3:#71717a;
  --purple:#7c3aed;--purple2:#6d28d9;--purple-glow:rgba(124,58,237,.15);
  --purple-dim:rgba(124,58,237,.08);--gradient:linear-gradient(135deg,#7c3aed,#a855f7);
  --green:#22c55e;--red:#ef4444;--teal:#2dd4bf;
}}
body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}}

/* Header */
.header{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 1.5rem;height:64px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}}
.logo{{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.15rem}}
.logo .icon{{width:30px;height:30px;background:var(--gradient);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px}}
.logo span{{color:var(--purple)}}
.timer{{font-family:'JetBrains Mono',monospace;font-size:1.4rem;font-weight:700;color:var(--purple);padding:.3rem .9rem;background:var(--purple-dim);border:1px solid rgba(124,58,237,.2);border-radius:.5rem;min-width:85px;text-align:center}}
.timer.warn{{color:var(--red);background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.2);animation:pulse 1s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}

/* Container */
.container{{max-width:760px;margin:0 auto;padding:2rem 1rem}}

/* Steps */
.step{{display:none}}.step.active{{display:block;animation:fadeIn .35s ease}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}

/* Card */
.card{{background:var(--surface);border:1px solid var(--border);border-radius:1.25rem;padding:2.25rem}}
.card h2{{font-size:1.75rem;font-weight:800;margin-bottom:.7rem}}
.card p{{color:var(--text2);line-height:1.7;margin-bottom:1rem}}

/* Info grid */
.info-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.875rem;margin:1.25rem 0}}
.info-item{{background:var(--surface2);border:1px solid var(--border);border-radius:.75rem;padding:1.1rem;text-align:center}}
.info-item .value{{font-family:'JetBrains Mono',monospace;font-size:1.6rem;font-weight:700;color:var(--purple)}}
.info-item .label{{font-size:.75rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;margin-top:.3rem}}

/* Form */
.form-group{{margin-bottom:1.1rem}}
.form-label{{display:block;font-weight:600;font-size:.8rem;color:var(--text2);margin-bottom:.4rem;text-transform:uppercase;letter-spacing:.04em}}
.form-input{{width:100%;padding:.8rem 1rem;background:var(--surface2);border:1.5px solid var(--border);border-radius:.65rem;color:var(--text);font-size:1rem;font-family:inherit;transition:all .2s}}
.form-input:focus{{outline:none;border-color:var(--purple);box-shadow:0 0 0 3px rgba(124,58,237,.1)}}
.form-input::placeholder{{color:var(--text3)}}

/* Buttons */
.btn{{display:inline-flex;align-items:center;justify-content:center;padding:.875rem 2rem;border:none;border-radius:.65rem;font-size:1rem;font-weight:700;cursor:pointer;transition:all .2s;font-family:inherit}}
.btn-primary{{background:var(--gradient);color:white}}
.btn-primary:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(124,58,237,.3)}}
.btn-primary:disabled{{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}}
.btn-secondary{{background:var(--surface2);color:var(--text);border:1px solid var(--border)}}
.btn-secondary:hover{{border-color:var(--border2)}}
.btn-submit{{background:linear-gradient(135deg,#059669,#047857);color:white}}
.btn-submit:hover{{box-shadow:0 8px 24px rgba(5,150,105,.25);transform:translateY(-2px)}}

/* Progress */
.progress-wrap{{margin-bottom:1.5rem}}
.progress-meta{{display:flex;justify-content:space-between;font-size:.8rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.4rem}}
.progress-bar{{height:5px;background:var(--surface3);border-radius:3px;overflow:hidden}}
.progress-fill{{height:100%;background:var(--gradient);border-radius:3px;transition:width .3s}}

/* Question */
.q-meta{{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.75rem}}
.q-badge{{padding:.2rem .55rem;border-radius:999px;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em}}
.q-badge.mcq{{background:#dbeafe;color:#1d4ed8}}
.q-badge.open{{background:#dcfce7;color:#15803d}}
.q-skill{{padding:.3rem .75rem;border-radius:.45rem;font-size:.72rem;font-weight:700;background:var(--surface2);color:var(--text2);border:1px solid var(--border)}}
.q-text{{font-size:1.35rem;font-weight:700;line-height:1.45;margin-bottom:1.75rem}}

/* Options */
.options{{display:flex;flex-direction:column;gap:.55rem;margin-bottom:1.5rem}}
.option{{display:flex;align-items:center;gap:1.1rem;padding:1.1rem 1.3rem;border:1.5px solid var(--border);border-radius:.875rem;background:var(--surface2);cursor:pointer;transition:all .2s;user-select:none}}
.option:hover{{border-color:var(--border2)}}
.option.selected{{border-color:var(--purple);background:var(--purple-dim)}}
.opt-letter{{width:34px;height:34px;border-radius:9px;border:1.5px solid var(--border2);background:var(--surface3);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:.875rem;color:var(--text2);flex-shrink:0;transition:all .2s}}
.option.selected .opt-letter{{background:var(--purple);border-color:var(--purple);color:white}}
.opt-text{{font-size:.95rem;font-weight:500;line-height:1.5}}

/* Text answer */
.text-answer{{width:100%;padding:1.1rem 1.25rem;background:var(--surface2);border:1.5px solid var(--border);border-radius:.875rem;font-size:.95rem;line-height:1.65;resize:vertical;min-height:160px;font-family:inherit;color:var(--text);transition:all .2s;margin-bottom:1.5rem}}
.text-answer:focus{{outline:none;border-color:var(--purple);box-shadow:0 0 0 3px rgba(124,58,237,.1)}}

/* Nav */
.nav-btns{{display:flex;justify-content:space-between;gap:1rem;margin-top:1.5rem}}
.q-nav{{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:1.5rem}}
.q-nav-btn{{width:34px;height:34px;border:1px solid var(--border);border-radius:.45rem;font-size:.78rem;font-weight:700;cursor:pointer;background:var(--surface2);color:var(--text3);transition:all .2s}}
.q-nav-btn.current{{background:var(--purple);color:white;border-color:var(--purple)}}
.q-nav-btn.answered{{background:rgba(45,212,191,.08);color:var(--teal);border-color:rgba(45,212,191,.2)}}

/* Tab warning */
.tab-warn{{position:fixed;top:0;left:0;right:0;background:var(--red);color:white;text-align:center;padding:.65rem;font-weight:700;font-size:.875rem;z-index:999;transform:translateY(-100%);transition:transform .3s}}
.tab-warn.show{{transform:translateY(0)}}

/* Overlay */
.overlay{{position:fixed;inset:0;background:rgba(10,10,11,.97);display:flex;align-items:center;justify-content:center;z-index:300;backdrop-filter:blur(10px)}}
.overlay-content{{text-align:center}}
.spinner{{width:48px;height:48px;border:4px solid var(--surface3);border-top-color:var(--purple);border-radius:50%;animation:spin .8s linear infinite;margin:1.5rem auto 0}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}

.result-icon{{font-size:3.5rem;margin-bottom:1rem}}
.result-score{{font-family:'JetBrains Mono',monospace;font-size:2.5rem;font-weight:700;color:var(--purple);margin:.5rem 0}}

.rules li{{padding:.4rem 0;color:var(--text2);font-size:.9375rem;display:flex;align-items:flex-start;gap:.5rem;list-style:none}}
.rules li::before{{content:'•';color:var(--purple);font-weight:700;flex-shrink:0}}

.hidden{{display:none!important}}
@media(max-width:600px){{.card{{padding:1.5rem}}.q-text{{font-size:1.15rem}}}}
</style>
</head>
<body>

<div class="tab-warn" id="tabWarn">⚠️ Tab switch detected! <span id="violationCount"></span></div>

<header class="header">
  <div class="logo"><div class="icon">⚡</div>Beat <span>Claude</span></div>
  <div class="timer" id="timer">--:--</div>
</header>

<!-- Step 1: Candidate Info -->
<div class="container">
<div class="step active" id="step-info">
  <div class="card">
    <h2>Welcome to the Assessment</h2>
    <p><strong>{title}</strong>{' — ' + role_title if role_title else ''}</p>
    <div class="info-grid">
      <div class="info-item"><div class="value">{num_questions}</div><div class="label">Questions</div></div>
      <div class="info-item"><div class="value">{duration}</div><div class="label">Minutes</div></div>
    </div>
    <ul class="rules" style="margin-bottom:1.5rem">
      <li>Do not switch tabs or windows — violations are recorded.</li>
      <li>Copy/paste is disabled during the exam.</li>
      <li>The exam auto-submits after 3 tab violations or when time runs out.</li>
      <li>You cannot re-take the exam with the same email.</li>
    </ul>
    <form id="infoForm">
      <div class="form-group">
        <label class="form-label">Full Name</label>
        <input type="text" class="form-input" id="cName" placeholder="Your full name" required>
      </div>
      <div class="form-group">
        <label class="form-label">Email</label>
        <input type="email" class="form-input" id="cEmail" placeholder="you@email.com" required>
      </div>
      <div class="form-group">
        <label class="form-label">Phone (optional)</label>
        <input type="tel" class="form-input" id="cPhone" placeholder="+91 98765 43210">
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%;margin-top:.5rem">Start Exam →</button>
    </form>
  </div>
</div>

<!-- Step 2: Questions -->
<div class="step" id="step-exam">
  <div class="progress-wrap">
    <div class="progress-meta">
      <span id="progressLabel">Question 1 of {num_questions}</span>
      <span id="progressPct">0%</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  </div>
  <div class="q-nav" id="qNav"></div>
  <div class="card" id="questionCard"></div>
  <div class="nav-btns">
    <button class="btn btn-secondary" id="prevBtn" onclick="navigate(-1)">← Previous</button>
    <button class="btn btn-submit" id="nextBtn" onclick="navigate(1)">Next →</button>
  </div>
</div>

<!-- Step 3: Result -->
<div class="step" id="step-result">
  <div class="card" style="text-align:center">
    <div class="result-icon">🎉</div>
    <h2>Submitted!</h2>
    <div class="result-score" id="resultScore"></div>
    <p style="margin-top:1rem">Your answers have been recorded. The recruiter will review your results and be in touch.</p>
  </div>
</div>
</div>

<!-- Submission overlay -->
<div class="overlay hidden" id="overlay">
  <div class="overlay-content">
    <span style="font-size:2.5rem">⏳</span>
    <p style="font-size:1.1rem;font-weight:600;margin-top:.75rem">Submitting your exam…</p>
    <div class="spinner"></div>
  </div>
</div>

<script>
const SLUG = "{slug}";
const QUESTIONS = {questions_json};
const DURATION_S = {duration} * 60;

let answers = {{}};
let currentQ = 0;
let tabViolations = 0;
let timerInterval = null;
let secondsLeft = DURATION_S;
let candidateName = '', candidateEmail = '', candidatePhone = '';

// ── Info Form ──
document.getElementById('infoForm').addEventListener('submit', e => {{
  e.preventDefault();
  candidateName  = document.getElementById('cName').value.trim();
  candidateEmail = document.getElementById('cEmail').value.trim();
  candidatePhone = document.getElementById('cPhone').value.trim();
  startExam();
}});

function startExam() {{
  showStep('step-exam');
  buildNav();
  renderQuestion(0);
  startTimer();
  document.addEventListener('visibilitychange', handleVisibility);
  document.addEventListener('contextmenu', e => e.preventDefault());
  document.addEventListener('copy', e => e.preventDefault());
  document.addEventListener('paste', e => e.preventDefault());
}}

function startTimer() {{
  timerInterval = setInterval(() => {{
    secondsLeft--;
    const m = Math.floor(secondsLeft / 60), s = secondsLeft % 60;
    const el = document.getElementById('timer');
    el.textContent = `${{m.toString().padStart(2,'0')}}:${{s.toString().padStart(2,'0')}}`;
    if (secondsLeft <= 300) el.classList.add('warn');
    if (secondsLeft <= 0) {{ clearInterval(timerInterval); submitExam('Time up!'); }}
  }}, 1000);
}}

function buildNav() {{
  const nav = document.getElementById('qNav');
  nav.innerHTML = QUESTIONS.map((_, i) =>
    `<button class="q-nav-btn${{i===0?' current':''}}" id="nb${{i}}" onclick="jumpTo(${{i}})">${{i+1}}</button>`
  ).join('');
}}

function renderQuestion(idx) {{
  const q = QUESTIONS[idx];
  const total = QUESTIONS.length;
  document.getElementById('progressLabel').textContent = `Question ${{idx+1}} of ${{total}}`;
  const pct = Math.round(idx / total * 100);
  document.getElementById('progressPct').textContent = pct + '%';
  document.getElementById('progressFill').style.width = pct + '%';

  document.querySelectorAll('.q-nav-btn').forEach((b,i) => {{
    b.className = 'q-nav-btn' + (i===idx?' current':'') + (answers[QUESTIONS[i].id]!==undefined?' answered':'');
  }});

  const isMCQ = q.type === 'MCQ';
  const badge = isMCQ ? '<span class="q-badge mcq">MCQ</span>' : '<span class="q-badge open">Open</span>';
  const skill = q.skill ? `<span class="q-skill">${{esc(q.skill)}}</span>` : '';
  let inputHtml = '';
  if (isMCQ) {{
    const letters = ['A','B','C','D'];
    inputHtml = '<div class="options">' + q.options.map((opt, i) =>
      `<div class="option${{answers[q.id]===letters[i]?' selected':''}}" onclick="selectMCQ(${{q.id}},'${{letters[i]}}',this)">
        <div class="opt-letter">${{letters[i]}}</div>
        <div class="opt-text">${{esc(opt)}}</div>
      </div>`
    ).join('') + '</div>';
  }} else {{
    const saved = answers[q.id] || '';
    inputHtml = `<textarea class="text-answer" id="ta_${{q.id}}" placeholder="Type your answer here…" oninput="saveText(${{q.id}})">${{esc(saved)}}</textarea>`;
  }}

  const isLast = idx === QUESTIONS.length - 1;
  document.getElementById('questionCard').innerHTML = `
    <div class="q-meta">${{badge}}${{skill}}</div>
    <div class="q-text">${{esc(q.question)}}</div>
    ${{inputHtml}}
  `;
  document.getElementById('prevBtn').disabled = idx === 0;
  document.getElementById('nextBtn').textContent = isLast ? '📤 Submit Exam' : 'Next →';
  document.getElementById('nextBtn').className = 'btn ' + (isLast ? 'btn-submit' : 'btn-primary');
  document.getElementById('nextBtn').onclick = isLast ? confirmSubmit : () => navigate(1);
  currentQ = idx;
}}

function selectMCQ(qid, letter, el) {{
  answers[qid] = letter;
  el.closest('.options').querySelectorAll('.option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  updateNav();
}}

function saveText(qid) {{
  const val = document.getElementById('ta_' + qid)?.value || '';
  if (val.trim()) answers[qid] = val;
  else delete answers[qid];
  updateNav();
}}

function updateNav() {{
  QUESTIONS.forEach((q, i) => {{
    const btn = document.getElementById('nb' + i);
    if (btn) btn.className = 'q-nav-btn' + (i===currentQ?' current':'') + (answers[q.id]!==undefined?' answered':'');
  }});
}}

function navigate(dir) {{
  const next = currentQ + dir;
  if (next >= 0 && next < QUESTIONS.length) renderQuestion(next);
}}
function jumpTo(idx) {{ renderQuestion(idx); }}

function confirmSubmit() {{
  const answered = Object.keys(answers).length;
  const total = QUESTIONS.length;
  if (answered < total && !confirm(`You've answered ${{answered}} of ${{total}} questions. Submit anyway?`)) return;
  submitExam('Submitted by candidate');
}}

async function submitExam(reason) {{
  clearInterval(timerInterval);
  document.getElementById('overlay').classList.remove('hidden');
  document.removeEventListener('visibilitychange', handleVisibility);

  const payload = {{
    name: candidateName, email: candidateEmail, phone: candidatePhone,
    answers: answers, tab_violations: tabViolations,
  }};
  try {{
    const res = await fetch(`/exam/${{SLUG}}/submit`, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload),
    }});
    const data = await res.json();
    document.getElementById('overlay').classList.add('hidden');
    document.getElementById('resultScore').textContent = data.mcq_score !== 'N/A' ? 'MCQ: ' + data.mcq_score : '✓';
    showStep('step-result');
  }} catch (e) {{
    document.getElementById('overlay').classList.add('hidden');
    alert('Submission failed. Please check your connection and try again.');
  }}
}}

function handleVisibility() {{
  if (document.hidden) {{
    tabViolations++;
    const el = document.getElementById('tabWarn');
    document.getElementById('violationCount').textContent = `(${{tabViolations}}/3)`;
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 3500);
    if (tabViolations >= 3) {{
      el.textContent = '🚫 Too many tab switches — auto-submitting.';
      el.classList.add('show');
      setTimeout(() => submitExam('Auto-submit: tab violations'), 1200);
    }}
  }}
}}

function showStep(id) {{
  document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}}

function esc(s) {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}
</script>
</body>
</html>"""
