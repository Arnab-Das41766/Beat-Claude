"""
Beat Claude — Cloud Backend
Handles candidate-facing exam links, MCQ auto-grading, and database storage.
Communicates with the local backend (via Cloudflare tunnel) for AI processing.
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import json
import secrets
import hashlib
import httpx
import os
from datetime import datetime
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

# Configuration
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
LOCAL_BACKEND_URL = os.getenv("LOCAL_BACKEND_URL", "http://localhost:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./exams.db")
CLOUD_APP_URL = os.getenv("CLOUD_APP_URL", "http://localhost:9000")

# Extract DB path from URL
DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite:///") else "exams.db"


# ============================================================================
# DATABASE
# ============================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            role_title TEXT DEFAULT '',
            questions_json TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 60,
            num_questions INTEGER DEFAULT 10,
            recruiter_email TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_exams_slug ON exams(slug)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT DEFAULT '',
            started_at TIMESTAMP,
            submitted_at TIMESTAMP,
            tab_violations INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (exam_slug) REFERENCES exams(slug),
            UNIQUE(exam_slug, email)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_candidates_slug ON candidates(exam_slug)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_option TEXT DEFAULT '',
            is_correct INTEGER DEFAULT 0,
            ai_score REAL DEFAULT -1,
            ai_feedback TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_answers_candidate ON answers(candidate_id)")

    conn.commit()
    conn.close()
    print("✅ Cloud database initialized!")


# ============================================================================
# APP SETUP
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Beat Claude Cloud API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def generate_slug(length: int = 12) -> str:
    """Generate a URL-safe random slug"""
    return secrets.token_urlsafe(length)[:length]


async def call_local_backend(endpoint: str, payload: dict, timeout: float = 180.0) -> dict:
    """Call the local backend via Cloudflare tunnel"""
    url = f"{LOCAL_BACKEND_URL.rstrip('/')}{endpoint}"
    headers = {"X-Internal-Key": INTERNAL_API_KEY, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Local backend is offline. Please start it and try again.")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Local backend timed out. LLM may be processing a large request.")
    except httpx.HTTPStatusError as e:
        detail = "Local backend error"
        try:
            detail = e.response.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=e.response.status_code, detail=detail)


# ============================================================================
# API ROUTES
# ============================================================================

@app.get("/")
async def root():
    return {"status": "ok", "message": "Beat Claude Cloud Backend", "docs": "/docs"}


@app.post("/api/create-exam")
async def create_exam(request: Request):
    """Create an exam by calling local backend for question generation"""
    data = await request.json()
    job_description = data.get("job_description", "")
    recruiter_email = data.get("recruiter_email", "")
    num_questions = data.get("num_questions", 10)
    duration_minutes = data.get("duration_minutes", 60)

    if not job_description or len(job_description) < 30:
        raise HTTPException(status_code=400, detail="Job description is too short (min 30 chars)")
    if not recruiter_email:
        raise HTTPException(status_code=400, detail="Recruiter email is required")

    # Call local backend to generate questions
    result = await call_local_backend("/generate-exam", {
        "job_description": job_description,
        "num_questions": num_questions,
    })

    questions = result.get("questions", [])
    jd_parsed = result.get("jd_parsed", {})

    if not questions:
        raise HTTPException(status_code=500, detail="Failed to generate questions")

    # Generate unique slug
    slug = generate_slug()
    conn = get_db()
    cursor = conn.cursor()

    # Ensure slug uniqueness
    while True:
        cursor.execute("SELECT id FROM exams WHERE slug = ?", (slug,))
        if not cursor.fetchone():
            break
        slug = generate_slug()

    title = jd_parsed.get("role_title", "Untitled") + " — Assessment"
    role_title = jd_parsed.get("role_title", "")

    cursor.execute("""
        INSERT INTO exams (slug, title, role_title, questions_json, duration_minutes, num_questions, recruiter_email)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        slug,
        title,
        role_title,
        json.dumps(questions),
        max(15, min(180, duration_minutes)),
        len(questions),
        recruiter_email.lower().strip(),
    ))
    conn.commit()
    conn.close()

    exam_link = f"{CLOUD_APP_URL.rstrip('/')}/exam/{slug}"

    return {
        "success": True,
        "slug": slug,
        "exam_link": exam_link,
        "title": title,
        "num_questions": len(questions),
    }


@app.get("/exam/{slug}", response_class=HTMLResponse)
async def exam_page(slug: str):
    """Serve the candidate-facing exam page"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = cursor.fetchone()
    conn.close()

    if not exam:
        return HTMLResponse(content=_error_html("Exam Not Found", "This exam link is invalid or has expired."), status_code=404)

    # Parse questions but NEVER send correct answers to frontend
    questions = json.loads(exam["questions_json"])
    safe_questions = []
    for q in questions:
        safe_q = {
            "id": q["id"],
            "question": q["question"],
            "options": q.get("options", []),
            "type": q.get("type", "MCQ"),
            "skill": q.get("skill", ""),
            "difficulty": q.get("difficulty", "medium"),
            "max_score": q.get("max_score", 10),
        }
        safe_questions.append(safe_q)

    return HTMLResponse(content=_exam_html(
        slug=slug,
        title=exam["title"],
        role_title=exam["role_title"],
        duration=exam["duration_minutes"],
        questions_json=json.dumps(safe_questions),
        num_questions=exam["num_questions"],
    ))


@app.post("/exam/{slug}/submit")
async def submit_exam(slug: str, request: Request):
    """Submit candidate answers, auto-grade MCQs, attempt AI grading for open-ended"""
    data = await request.json()
    candidate_name = data.get("name", "").strip()
    candidate_email = data.get("email", "").strip().lower()
    candidate_phone = data.get("phone", "").strip()
    submitted_answers = data.get("answers", {})  # { "question_id": "selected_option" }
    tab_violations = data.get("tab_violations", 0)

    if not candidate_name or not candidate_email:
        raise HTTPException(status_code=400, detail="Name and email are required")

    conn = get_db()
    cursor = conn.cursor()

    # Get exam
    cursor.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = cursor.fetchone()
    if not exam:
        conn.close()
        raise HTTPException(status_code=404, detail="Exam not found")

    # Check if already submitted
    cursor.execute("SELECT id, submitted_at FROM candidates WHERE exam_slug = ? AND email = ?", (slug, candidate_email))
    existing = cursor.fetchone()
    if existing and existing["submitted_at"]:
        conn.close()
        raise HTTPException(status_code=400, detail="You have already submitted this exam")

    # Create or update candidate
    if existing:
        candidate_id = existing["id"]
        cursor.execute(
            "UPDATE candidates SET submitted_at = CURRENT_TIMESTAMP, tab_violations = ? WHERE id = ?",
            (tab_violations, candidate_id)
        )
    else:
        cursor.execute(
            "INSERT INTO candidates (exam_slug, name, email, phone, started_at, submitted_at, tab_violations) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (slug, candidate_name, candidate_email, candidate_phone, tab_violations)
        )
        candidate_id = cursor.lastrowid

    # Load questions with correct answers
    questions = json.loads(exam["questions_json"])
    questions_map = {str(q["id"]): q for q in questions}

    mcq_correct = 0
    mcq_total = 0
    open_ended_pending = []

    for q_id_str, selected in submitted_answers.items():
        q = questions_map.get(q_id_str)
        if not q:
            continue

        is_correct = 0
        ai_score = -1.0
        ai_feedback = ""

        if q.get("type", "MCQ") == "MCQ":
            mcq_total += 1
            correct_ans = (q.get("correct_answer", "") or "").upper().strip()
            selected_upper = (selected or "").upper().strip()
            if correct_ans and selected_upper == correct_ans:
                is_correct = 1
                mcq_correct += 1
        else:
            # Open-ended — will try AI grading below
            open_ended_pending.append({
                "q_id": q_id_str,
                "question": q.get("question", ""),
                "guidelines": q.get("guidelines", ""),
                "candidate_answer": selected or "",
            })

        cursor.execute("""
            INSERT INTO answers (candidate_id, question_id, selected_option, is_correct, ai_score, ai_feedback)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (candidate_id, int(q_id_str), selected or "", is_correct, ai_score, ai_feedback))

    conn.commit()

    # Attempt AI grading for open-ended questions (non-blocking, best-effort)
    for item in open_ended_pending:
        try:
            grade_result = await call_local_backend("/grade-open-ended", {
                "question": item["question"],
                "candidate_answer": item["candidate_answer"],
                "ideal_hints": item["guidelines"],
            }, timeout=60.0)

            cursor.execute("""
                UPDATE answers SET ai_score = ?, ai_feedback = ?
                WHERE candidate_id = ? AND question_id = ?
            """, (
                grade_result.get("score", -1),
                grade_result.get("feedback", ""),
                candidate_id,
                int(item["q_id"]),
            ))
        except Exception as e:
            # Local backend offline — mark as pending
            cursor.execute("""
                UPDATE answers SET ai_score = -1, ai_feedback = 'Pending AI grading — local backend was offline'
                WHERE candidate_id = ? AND question_id = ?
            """, (candidate_id, int(item["q_id"])))
            print(f"⚠️  AI grading failed for Q{item['q_id']}: {e}")

    conn.commit()
    conn.close()

    return {
        "status": "submitted",
        "mcq_score": f"{mcq_correct}/{mcq_total}" if mcq_total > 0 else "N/A",
        "candidate_id": candidate_id,
    }


@app.get("/recruiter/exams")
async def get_recruiter_exams(email: str = ""):
    """Return all exams created by a recruiter, with candidate stats"""
    if not email:
        raise HTTPException(status_code=400, detail="Email parameter is required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM exams WHERE LOWER(recruiter_email) = LOWER(?) ORDER BY created_at DESC
    """, (email.strip(),))
    exams = [dict(row) for row in cursor.fetchall()]

    result = []
    for exam in exams:
        slug = exam["slug"]
        # Count candidates
        cursor.execute("SELECT COUNT(*) as cnt FROM candidates WHERE exam_slug = ? AND submitted_at IS NOT NULL", (slug,))
        candidate_count = cursor.fetchone()["cnt"]

        # Calculate average score
        avg_score = 0
        if candidate_count > 0:
            questions = json.loads(exam["questions_json"])
            mcq_total = sum(1 for q in questions if q.get("type", "MCQ") == "MCQ")

            cursor.execute("""
                SELECT c.id FROM candidates c WHERE c.exam_slug = ? AND c.submitted_at IS NOT NULL
            """, (slug,))
            cand_ids = [r["id"] for r in cursor.fetchall()]

            scores = []
            for cid in cand_ids:
                cursor.execute("SELECT SUM(is_correct) as correct FROM answers WHERE candidate_id = ?", (cid,))
                row = cursor.fetchone()
                correct = row["correct"] or 0
                total = exam["num_questions"]
                scores.append(round(correct / total * 100) if total > 0 else 0)
            avg_score = round(sum(scores) / len(scores)) if scores else 0

        result.append({
            "slug": slug,
            "title": exam["title"],
            "role_title": exam["role_title"],
            "num_questions": exam["num_questions"],
            "duration_minutes": exam["duration_minutes"],
            "candidate_count": candidate_count,
            "avg_score": avg_score,
            "created_at": exam["created_at"],
            "exam_link": f"{CLOUD_APP_URL.rstrip('/')}/exam/{slug}",
        })

    conn.close()
    return {"success": True, "exams": result}


@app.get("/recruiter/results/{slug}")
async def get_recruiter_results(slug: str):
    """Return full results for an exam — all candidates, scores, answers with question details"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM exams WHERE slug = ?", (slug,))
    exam = cursor.fetchone()
    if not exam:
        conn.close()
        raise HTTPException(status_code=404, detail="Exam not found")

    cursor.execute("""
        SELECT * FROM candidates WHERE exam_slug = ? ORDER BY submitted_at DESC
    """, (slug,))
    candidates = [dict(row) for row in cursor.fetchall()]

    questions = json.loads(exam["questions_json"])
    questions_map = {q["id"]: q for q in questions}

    for candidate in candidates:
        cursor.execute("""
            SELECT * FROM answers WHERE candidate_id = ? ORDER BY question_id
        """, (candidate["id"],))
        raw_answers = [dict(row) for row in cursor.fetchall()]

        # Enrich each answer with the original question details
        enriched_answers = []
        for a in raw_answers:
            q = questions_map.get(a["question_id"], {})
            enriched_answers.append({
                "question_id": a["question_id"],
                "question_text": q.get("question", ""),
                "question_type": q.get("type", "MCQ"),
                "options": q.get("options", []),
                "correct_answer": q.get("correct_answer", ""),
                "skill": q.get("skill", ""),
                "difficulty": q.get("difficulty", "medium"),
                "max_score": q.get("max_score", 10),
                "candidate_answer": a["selected_option"],
                "is_correct": a["is_correct"],
                "ai_score": a["ai_score"],
                "ai_feedback": a["ai_feedback"],
            })
        candidate["answers"] = enriched_answers

        # Calculate scores
        mcq_correct = sum(1 for a in enriched_answers if a["is_correct"])
        mcq_total = sum(1 for q in questions if q.get("type", "MCQ") == "MCQ")
        ai_scored = [a for a in enriched_answers if a["ai_score"] >= 0]
        ai_avg = sum(a["ai_score"] for a in ai_scored) / len(ai_scored) if ai_scored else 0

        candidate["mcq_score"] = f"{mcq_correct}/{mcq_total}" if mcq_total > 0 else "N/A"
        candidate["mcq_correct"] = mcq_correct
        candidate["mcq_total"] = mcq_total
        candidate["ai_average"] = round(ai_avg, 1)
        candidate["total_questions"] = len(questions)

    conn.close()

    return {
        "success": True,
        "exam": {
            "slug": exam["slug"],
            "title": exam["title"],
            "role_title": exam["role_title"],
            "num_questions": exam["num_questions"],
            "duration_minutes": exam["duration_minutes"],
            "recruiter_email": exam["recruiter_email"],
            "created_at": exam["created_at"],
        },
        "questions": questions,
        "candidates": candidates,
    }


# ============================================================================
# HTML TEMPLATES
# ============================================================================

def _error_html(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Beat Claude</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,700;1,700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Inter',sans-serif; background:#0c0c0e; color:#e4e4e7; min-height:100vh; display:flex; align-items:center; justify-content:center; }}
  .card {{ background:#18181b; border:1px solid #27272a; border-radius:1.25rem; padding:3rem; max-width:480px; text-align:center; }}
  h1 {{ font-family:'Playfair Display',serif; font-size:2rem; color:#d4a847; margin-bottom:1rem; }}
  p {{ color:#a1a1aa; font-size:1rem; line-height:1.6; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""


def _exam_html(slug: str, title: str, role_title: str, duration: int, questions_json: str, num_questions: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Beat Claude Exam</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,700;1,700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root {{
    --bg: #0c0c0e; --surface: #18181b; --surface2: #1f1f23; --surface3: #27272a;
    --border: #27272a; --border2: #3f3f46; --text: #e4e4e7; --text2: #a1a1aa;
    --text3: #71717a; --gold: #d4a847; --gold2: #c9952e; --gold-dim: rgba(212,168,71,0.08);
    --gold-glow: rgba(212,168,71,0.06); --teal: #2cc4a4; --teal-dim: rgba(44,196,164,0.08);
    --rose: #f43f5e; --violet: #8b5cf6;
  }}
  body {{ font-family:'Inter',sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }}

  /* ── Header ── */
  .header {{ background:var(--surface); border-bottom:1px solid var(--border); padding:0 1.5rem; height:64px; display:flex; align-items:center; justify-content:space-between; position:sticky; top:0; z-index:100; }}
  .header-left {{ display:flex; align-items:center; gap:.75rem; }}
  .logo-icon {{ width:32px; height:32px; background:var(--gold); border-radius:8px; display:flex; align-items:center; justify-content:center; color:#0c0c0e; font-size:16px; }}
  .logo-text {{ font-weight:700; font-size:1.25rem; font-family:'Playfair Display',serif; }}
  .logo-text span {{ color:var(--gold); font-style:italic; }}
  .header-right {{ display:flex; align-items:center; gap:1.5rem; }}
  .timer {{ font-family:'JetBrains Mono',monospace; font-size:1.5rem; font-weight:700; color:var(--gold); padding:.35rem 1rem; background:var(--gold-dim); border:1px solid rgba(212,168,71,.2); border-radius:.5rem; min-width:90px; text-align:center; }}
  .timer.warning {{ color:var(--rose); background:rgba(244,63,94,.08); border-color:rgba(244,63,94,.2); animation:pulse 1s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.65}} }}

  /* ── Container ── */
  .container {{ max-width:800px; margin:0 auto; padding:2rem 1.5rem; }}

  /* ── Step panels ── */
  .step {{ display:none; }}
  .step.active {{ display:block; animation:fadeIn .4s ease; }}
  @keyframes fadeIn {{ from{{opacity:0;transform:translateY(12px)}} to{{opacity:1;transform:translateY(0)}} }}

  /* ── Card ── */
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:1.25rem; padding:2.5rem; }}
  .card h2 {{ font-family:'Playfair Display',serif; font-size:2rem; margin-bottom:.75rem; }}
  .card h2 span {{ color:var(--gold); font-style:italic; }}
  .card p {{ color:var(--text2); line-height:1.7; margin-bottom:1rem; }}

  /* ── Form ── */
  .form-group {{ margin-bottom:1.25rem; }}
  .form-label {{ display:block; font-weight:600; font-size:.875rem; color:var(--text2); margin-bottom:.5rem; text-transform:uppercase; letter-spacing:.05em; }}
  .form-input {{ width:100%; padding:.875rem 1rem; background:var(--surface2); border:1.5px solid var(--border); border-radius:.75rem; color:var(--text); font-size:1rem; font-family:inherit; transition:all .2s; }}
  .form-input:focus {{ outline:none; border-color:var(--gold); box-shadow:0 0 0 3px var(--gold-glow); }}
  .form-input::placeholder {{ color:var(--text3); }}

  /* ── Buttons ── */
  .btn {{ display:inline-flex; align-items:center; justify-content:center; padding:.875rem 2rem; border:none; border-radius:.75rem; font-size:1rem; font-weight:700; cursor:pointer; transition:all .2s; font-family:inherit; }}
  .btn-primary {{ background:linear-gradient(135deg,var(--gold),var(--gold2)); color:#0c0c0e; }}
  .btn-primary:hover {{ transform:translateY(-2px); box-shadow:0 8px 24px rgba(212,168,71,.25); }}
  .btn-primary:disabled {{ opacity:.5; cursor:not-allowed; transform:none; box-shadow:none; }}
  .btn-secondary {{ background:var(--surface2); color:var(--text); border:1px solid var(--border); }}
  .btn-secondary:hover {{ border-color:var(--border2); background:var(--surface3); }}
  .btn-lg {{ padding:1rem 2.5rem; font-size:1.125rem; }}
  .btn-submit {{ background:linear-gradient(135deg,#059669,#047857); color:white; }}
  .btn-submit:hover {{ box-shadow:0 8px 24px rgba(5,150,105,.25); transform:translateY(-2px); }}

  /* ── Instructions ── */
  .info-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:1rem; margin:1.5rem 0; }}
  .info-item {{ background:var(--surface2); border:1px solid var(--border); border-radius:.75rem; padding:1.25rem; text-align:center; }}
  .info-item .value {{ font-family:'JetBrains Mono',monospace; font-size:1.75rem; font-weight:700; color:var(--gold); }}
  .info-item .label {{ font-size:.75rem; color:var(--text3); text-transform:uppercase; letter-spacing:.05em; margin-top:.35rem; }}
  .rules {{ list-style:none; }}
  .rules li {{ padding:.5rem 0; color:var(--text2); font-size:.9375rem; display:flex; align-items:flex-start; gap:.5rem; }}
  .rules li::before {{ content:'•'; color:var(--gold); font-weight:700; flex-shrink:0; }}

  /* ── Progress ── */
  .progress-bar {{ height:6px; background:var(--surface3); border-radius:3px; overflow:hidden; margin-bottom:1.5rem; }}
  .progress-fill {{ height:100%; background:linear-gradient(90deg,var(--gold),var(--gold2)); border-radius:3px; transition:width .3s; }}
  .progress-label {{ display:flex; justify-content:space-between; font-size:.8125rem; color:var(--text3); font-weight:700; text-transform:uppercase; letter-spacing:.05em; margin-bottom:.5rem; }}

  /* ── Question ── */
  .q-meta {{ display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; margin-bottom:.75rem; }}
  .q-number {{ font-size:.85rem; color:var(--text3); }}
  .q-badge {{ padding:.2rem .6rem; border-radius:9999px; font-size:.7rem; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }}
  .q-badge.mcq {{ background:#dbeafe; color:#1d4ed8; }}
  .q-badge.open {{ background:#f0fdf4; color:#15803d; }}
  .q-skill {{ padding:.35rem .875rem; border-radius:.5rem; font-size:.75rem; font-weight:700; background:var(--surface2); color:var(--text2); border:1px solid var(--border); }}
  .q-points {{ font-size:.8125rem; font-weight:600; color:var(--text3); margin-left:auto; }}
  .q-text {{ font-family:'Playfair Display',serif; font-size:1.5rem; font-weight:700; line-height:1.4; margin-bottom:2rem; }}

  /* ── MCQ Options ── */
  .options {{ display:flex; flex-direction:column; gap:.65rem; margin-bottom:1.5rem; }}
  .option {{ display:flex; align-items:center; gap:1.25rem; padding:1.25rem 1.5rem; border:1.5px solid var(--border); border-radius:.875rem; background:var(--surface2); cursor:pointer; transition:all .2s; user-select:none; }}
  .option:hover {{ border-color:var(--border2); background:var(--surface3); }}
  .option.selected {{ border-color:var(--gold); background:var(--gold-dim); }}
  .option-letter {{ width:36px; height:36px; border-radius:10px; border:1.5px solid var(--border2); background:var(--surface3); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:.9375rem; color:var(--text2); flex-shrink:0; transition:all .2s; }}
  .option.selected .option-letter {{ background:var(--gold); border-color:var(--gold); color:#0c0c0e; }}
  .option-text {{ font-size:1rem; font-weight:500; color:var(--text); line-height:1.5; }}

  /* ── Text answer ── */
  .text-answer {{ width:100%; padding:1.25rem 1.5rem; background:var(--surface2); border:1.5px solid var(--border); border-radius:.875rem; font-size:1rem; line-height:1.6; resize:vertical; min-height:180px; font-family:inherit; color:var(--text); transition:all .2s; margin-bottom:1.5rem; }}
  .text-answer:focus {{ outline:none; border-color:var(--gold); box-shadow:0 0 0 3px var(--gold-glow); }}

  /* ── Navigation ── */
  .nav-buttons {{ display:flex; justify-content:space-between; gap:1rem; margin-top:1.5rem; }}

  /* ── Question sidebar nav ── */
  .q-nav {{ display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:1.5rem; }}
  .q-nav-btn {{ width:36px; height:36px; border:1px solid var(--border); border-radius:.5rem; font-size:.8rem; font-weight:700; cursor:pointer; background:var(--surface2); color:var(--text3); transition:all .2s; }}
  .q-nav-btn:hover {{ background:var(--surface3); color:var(--text); }}
  .q-nav-btn.current {{ background:var(--gold); color:#0c0c0e; border-color:var(--gold); }}
  .q-nav-btn.answered {{ background:var(--teal-dim); color:var(--teal); border-color:rgba(44,196,164,.2); }}

  /* ── Tab violation warning ── */
  .tab-warning {{ position:fixed; top:0; left:0; right:0; background:var(--rose); color:white; text-align:center; padding:.75rem; font-weight:700; font-size:.875rem; z-index:999; transform:translateY(-100%); transition:transform .3s; }}
  .tab-warning.show {{ transform:translateY(0); }}

  /* ── Submission overlay ── */
  .overlay {{ position:fixed; inset:0; background:rgba(12,12,14,.98); display:flex; align-items:center; justify-content:center; z-index:300; backdrop-filter:blur(12px); }}
  .overlay-content {{ text-align:center; }}
  .overlay-icon {{ font-size:3rem; margin-bottom:1rem; display:block; animation:bounce 1s infinite; }}
  @keyframes bounce {{ 0%,100%{{transform:translateY(0)}} 50%{{transform:translateY(-8px)}} }}
  .spinner {{ width:48px; height:48px; border:4px solid var(--surface3); border-top-color:var(--gold); border-radius:50%; animation:spin .8s linear infinite; margin:1.5rem auto 0; }}
  @keyframes spin {{ to{{transform:rotate(360deg)}} }}

  /* ── Result card ── */
  .result-icon {{ font-size:4rem; margin-bottom:1rem; }}
  .result-score {{ font-family:'JetBrains Mono',monospace; font-size:3rem; font-weight:700; color:var(--gold); margin:.5rem 0; }}

  .hidden {{ display:none !important; }}

  @media(max-width:640px) {{
    .card {{ padding:1.5rem; }}
    .q-text {{ font-size:1.25rem; }}
    .info-grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>
  <!-- Tab violation warning banner -->
  <div id="tabWarning" class="tab-warning">⚠️ Tab switch detected! <span id="tabWarningText"></span></div>

  <!-- Header -->
  <header class="header">
    <div class="header-left">
      <div class="logo-icon">⚗</div>
      <span class="logo-text">Beat <span>Claude</span></span>
    </div>
    <div class="header-right">
      <div id="timer" class="timer hidden">--:--</div>
    </div>
  </header>

  <div class="container">
    <!-- STEP 1: Registration -->
    <div id="step1" class="step active">
      <div class="card">
        <h2>Welcome to <span>{role_title or title}</span></h2>
        <p>Please enter your details to begin the assessment. Your information will be shared with the recruiter.</p>

        <form id="regForm" onsubmit="return false;">
          <div class="form-group">
            <label class="form-label">Full Name *</label>
            <input type="text" id="candName" class="form-input" placeholder="Enter your full name" required autocomplete="name">
          </div>
          <div class="form-group">
            <label class="form-label">Email Address *</label>
            <input type="email" id="candEmail" class="form-input" placeholder="you@email.com" required autocomplete="email">
          </div>
          <div class="form-group">
            <label class="form-label">Phone <span style="color:var(--text3)">(optional)</span></label>
            <input type="tel" id="candPhone" class="form-input" placeholder="+1 234 567 8900" autocomplete="tel">
          </div>
          <div id="regError" style="color:var(--rose);font-size:.875rem;margin-bottom:1rem;display:none;"></div>
          <button type="button" class="btn btn-primary btn-lg" style="width:100%" onclick="goToInstructions()">Continue →</button>
        </form>
      </div>
    </div>

    <!-- STEP 2: Instructions -->
    <div id="step2" class="step">
      <div class="card">
        <h2>Before You <span>Begin</span></h2>
        <p>Please read the following instructions carefully before starting.</p>

        <div class="info-grid">
          <div class="info-item">
            <div class="value">{num_questions}</div>
            <div class="label">Questions</div>
          </div>
          <div class="info-item">
            <div class="value">{duration}</div>
            <div class="label">Minutes</div>
          </div>
          <div class="info-item">
            <div class="value">1</div>
            <div class="label">Attempt</div>
          </div>
        </div>

        <ul class="rules">
          <li>Answer all questions within the time limit</li>
          <li>You can navigate between questions freely</li>
          <li>Your answers are saved automatically as you go</li>
          <li>Do not switch tabs or leave this window</li>
          <li>After 3 tab switches, your exam will auto-submit</li>
          <li>Right-click, copy, and paste are disabled during the exam</li>
          <li>Once submitted, you cannot change your answers</li>
        </ul>

        <div style="margin-top:2rem;display:flex;gap:1rem;">
          <button class="btn btn-secondary" onclick="showStep(1)">← Back</button>
          <button class="btn btn-primary btn-lg" style="flex:1" onclick="startExam()">Start Exam →</button>
        </div>
      </div>
    </div>

    <!-- STEP 3: Exam Questions -->
    <div id="step3" class="step">
      <div class="card">
        <!-- Progress -->
        <div class="progress-label">
          <span>Progress</span>
          <span id="progressPct">0%</span>
        </div>
        <div class="progress-bar">
          <div id="progressFill" class="progress-fill" style="width:0%"></div>
        </div>

        <!-- Question nav -->
        <div id="qNav" class="q-nav"></div>

        <!-- Question content -->
        <div class="q-meta">
          <span id="qNum" class="q-number"></span>
          <span id="qBadge" class="q-badge"></span>
          <span id="qSkill" class="q-skill"></span>
          <span id="qPts" class="q-points"></span>
        </div>
        <div id="qText" class="q-text"></div>

        <!-- MCQ options -->
        <div id="mcqBox" class="options hidden"></div>

        <!-- Text answer -->
        <textarea id="textBox" class="text-answer hidden" placeholder="Type your answer here..."></textarea>

        <!-- Navigation -->
        <div class="nav-buttons">
          <button id="prevBtn" class="btn btn-secondary" onclick="prevQ()" disabled>← Previous</button>
          <button id="nextBtn" class="btn btn-primary" onclick="nextQ()">Next →</button>
        </div>

        <div style="text-align:center;margin-top:1.5rem;">
          <button class="btn btn-submit btn-lg" onclick="confirmSubmit()">✓ Submit Exam</button>
          <p style="font-size:.8rem;color:var(--text3);margin-top:.5rem;">Make sure you've answered all questions</p>
        </div>
      </div>
    </div>

    <!-- STEP 4: Submitted -->
    <div id="step4" class="step">
      <div class="card" style="text-align:center;">
        <div class="result-icon">🎉</div>
        <h2>Exam <span>Submitted!</span></h2>
        <div id="resultScore" class="result-score"></div>
        <p>Your answers have been submitted successfully. The recruiter will review your results.</p>
        <p style="color:var(--text3);font-size:.875rem;margin-top:1.5rem;">You may close this tab now.</p>
      </div>
    </div>
  </div>

  <!-- Submitting overlay -->
  <div id="submitOverlay" class="overlay hidden">
    <div class="overlay-content">
      <span class="overlay-icon">🚀</span>
      <h2 style="font-size:1.5rem;font-weight:800;margin-bottom:.75rem;">Submitting your answers...</h2>
      <p style="color:var(--text2);">Please wait, do not close this tab.</p>
      <div class="spinner"></div>
    </div>
  </div>

  <!-- Confirm modal -->
  <div id="confirmModal" class="overlay hidden" style="background:rgba(12,12,14,.7)">
    <div class="card" style="max-width:420px;text-align:center;">
      <div style="font-size:3rem;margin-bottom:1rem;">⚠️</div>
      <h2 style="font-size:1.5rem;font-weight:800;margin-bottom:.75rem;">Submit Exam?</h2>
      <p>You have answered <strong id="modalAnswered">0</strong> of <strong id="modalTotal">0</strong> questions. This action cannot be undone.</p>
      <div style="display:flex;gap:.75rem;margin-top:1.5rem;">
        <button class="btn btn-secondary" style="flex:1" onclick="closeConfirm()">Cancel</button>
        <button class="btn btn-submit" style="flex:1" onclick="doSubmit()">Confirm & Submit</button>
      </div>
    </div>
  </div>

<script>
// ── Configuration ──
const SLUG = "{slug}";
const DURATION = {duration} * 60; // seconds
const QUESTIONS = {questions_json};

// ── State ──
let currentStep = 1;
let currentQ = 0;
let answers = {{}};     // {{ questionId: selectedOption }}
let timeLeft = DURATION;
let timerInterval = null;
let tabViolations = 0;
let examStarted = false;
let submitted = false;

// ── Randomize question order by email seed ──
let orderedQuestions = [...QUESTIONS];

function seedShuffle(arr, seed) {{
  let s = 0;
  for (let i = 0; i < seed.length; i++) s = ((s << 5) - s + seed.charCodeAt(i)) | 0;
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {{
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    const j = s % (i + 1);
    [a[i], a[j]] = [a[j], a[i]];
  }}
  return a;
}}

// ── Step management ──
function showStep(n) {{
  for (let i = 1; i <= 4; i++) {{
    const el = document.getElementById('step' + i);
    el.classList.remove('active');
  }}
  document.getElementById('step' + n).classList.add('active');
  currentStep = n;
}}

function goToInstructions() {{
  const name = document.getElementById('candName').value.trim();
  const email = document.getElementById('candEmail').value.trim();
  if (!name || !email) {{
    const err = document.getElementById('regError');
    err.textContent = 'Please fill in your name and email.';
    err.style.display = 'block';
    return;
  }}
  // Shuffle questions based on email
  orderedQuestions = seedShuffle(QUESTIONS, email);
  showStep(2);
}}

function startExam() {{
  examStarted = true;
  document.getElementById('timer').classList.remove('hidden');
  buildQNav();
  showQuestion(0);
  showStep(3);
  startTimer();
  enableAntiCheat();
}}

// ── Timer ──
function startTimer() {{
  updateTimerDisplay();
  timerInterval = setInterval(() => {{
    timeLeft--;
    updateTimerDisplay();
    if (timeLeft <= 0) {{
      clearInterval(timerInterval);
      doSubmit();
    }}
  }}, 1000);
}}

function updateTimerDisplay() {{
  const m = Math.floor(Math.max(0, timeLeft) / 60);
  const s = Math.max(0, timeLeft) % 60;
  const el = document.getElementById('timer');
  el.textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  if (timeLeft <= 300) el.classList.add('warning');
}}

// ── Question navigation ──
function buildQNav() {{
  const nav = document.getElementById('qNav');
  nav.innerHTML = orderedQuestions.map((q, i) =>
    `<button class="q-nav-btn" id="qnav${{i}}" onclick="goToQ(${{i}})">${{i+1}}</button>`
  ).join('');
}}

function updateQNav() {{
  orderedQuestions.forEach((q, i) => {{
    const btn = document.getElementById('qnav' + i);
    if (!btn) return;
    btn.className = 'q-nav-btn';
    if (answers[q.id] !== undefined) btn.classList.add('answered');
    if (i === currentQ) btn.classList.add('current');
  }});
  const answered = Object.keys(answers).length;
  const pct = orderedQuestions.length > 0 ? Math.round(answered / orderedQuestions.length * 100) : 0;
  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('progressPct').textContent = pct + '%';
}}

function showQuestion(idx) {{
  saveCurrentAnswer();
  currentQ = idx;
  const q = orderedQuestions[idx];
  document.getElementById('qNum').textContent = `Question ${{idx+1}} of ${{orderedQuestions.length}}`;

  const badge = document.getElementById('qBadge');
  const isMCQ = q.type === 'MCQ';
  badge.className = 'q-badge ' + (isMCQ ? 'mcq' : 'open');
  badge.textContent = q.type.replace('_', ' ');

  document.getElementById('qSkill').textContent = q.skill || '';
  document.getElementById('qPts').textContent = (q.max_score || 10) + ' pts';
  document.getElementById('qText').textContent = q.question;

  const mcqBox = document.getElementById('mcqBox');
  const textBox = document.getElementById('textBox');

  if (isMCQ && q.options && q.options.length > 0) {{
    mcqBox.classList.remove('hidden');
    textBox.classList.add('hidden');
    const letters = ['A','B','C','D','E','F'];
    const sel = answers[q.id] || '';
    mcqBox.innerHTML = q.options.map((opt, i) => `
      <div class="option ${{sel === letters[i] ? 'selected' : ''}}" onclick="selectOpt(${{q.id}},'${{letters[i]}}')">
        <div class="option-letter">${{letters[i]}}</div>
        <div class="option-text">${{opt.replace(/^[A-F][\\.\\)]\\s*/, '')}}</div>
      </div>
    `).join('');
  }} else {{
    mcqBox.classList.add('hidden');
    textBox.classList.remove('hidden');
    textBox.value = answers[q.id] || '';
  }}

  document.getElementById('prevBtn').disabled = idx === 0;
  document.getElementById('nextBtn').textContent = idx === orderedQuestions.length - 1 ? 'Review ✓' : 'Next →';
  updateQNav();
}}

function saveCurrentAnswer() {{
  if (!examStarted || orderedQuestions.length === 0) return;
  const q = orderedQuestions[currentQ];
  if (!q) return;
  if (q.type === 'MCQ') {{
    // Already saved via selectOpt
  }} else {{
    const val = document.getElementById('textBox').value.trim();
    if (val) answers[q.id] = val;
    else delete answers[q.id];
  }}
}}

function selectOpt(qId, letter) {{
  answers[qId] = letter;
  showQuestion(currentQ); // refresh UI
}}

function goToQ(idx) {{ showQuestion(idx); }}
function prevQ() {{ if (currentQ > 0) showQuestion(currentQ - 1); }}
function nextQ() {{ if (currentQ < orderedQuestions.length - 1) showQuestion(currentQ + 1); }}

// ── Submit ──
function confirmSubmit() {{
  saveCurrentAnswer();
  document.getElementById('modalAnswered').textContent = Object.keys(answers).length;
  document.getElementById('modalTotal').textContent = orderedQuestions.length;
  document.getElementById('confirmModal').classList.remove('hidden');
}}

function closeConfirm() {{
  document.getElementById('confirmModal').classList.add('hidden');
}}

async function doSubmit() {{
  if (submitted) return;
  submitted = true;
  clearInterval(timerInterval);
  saveCurrentAnswer();
  document.getElementById('confirmModal').classList.add('hidden');
  document.getElementById('submitOverlay').classList.remove('hidden');

  const payload = {{
    name: document.getElementById('candName').value.trim(),
    email: document.getElementById('candEmail').value.trim().toLowerCase(),
    phone: document.getElementById('candPhone').value.trim(),
    answers: answers,
    tab_violations: tabViolations,
  }};

  try {{
    const resp = await fetch('/exam/' + SLUG + '/submit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload),
    }});
    const data = await resp.json();

    document.getElementById('submitOverlay').classList.add('hidden');

    if (resp.ok) {{
      document.getElementById('resultScore').textContent = data.mcq_score || '';
      showStep(4);
    }} else {{
      alert(data.detail || 'Submission failed. Please try again.');
      submitted = false;
    }}
  }} catch (err) {{
    document.getElementById('submitOverlay').classList.add('hidden');
    alert('Network error. Please check your connection and try again.');
    submitted = false;
  }}
}}

// ── Anti-cheat ──
function enableAntiCheat() {{
  // Tab switch detection
  document.addEventListener('visibilitychange', () => {{
    if (document.hidden && examStarted && !submitted) {{
      tabViolations++;
      const warning = document.getElementById('tabWarning');
      if (tabViolations >= 3) {{
        warning.querySelector('#tabWarningText').textContent = 'Auto-submitting your exam now.';
        warning.classList.add('show');
        setTimeout(() => doSubmit(), 1500);
      }} else {{
        warning.querySelector('#tabWarningText').textContent =
          `Warning ${{tabViolations}}/3 — your exam will auto-submit after ${{3 - tabViolations}} more.`;
        warning.classList.add('show');
        setTimeout(() => warning.classList.remove('show'), 4000);
      }}
    }}
  }});

  // Disable right-click
  document.addEventListener('contextmenu', (e) => {{ if (examStarted && !submitted) e.preventDefault(); }});

  // Disable copy/paste
  document.addEventListener('copy', (e) => {{ if (examStarted && !submitted) e.preventDefault(); }});
  document.addEventListener('paste', (e) => {{ if (examStarted && !submitted) e.preventDefault(); }});
  document.addEventListener('cut', (e) => {{ if (examStarted && !submitted) e.preventDefault(); }});

  // Disable common shortcuts
  document.addEventListener('keydown', (e) => {{
    if (!examStarted || submitted) return;
    if ((e.ctrlKey || e.metaKey) && ['c','v','x','a','u','s','p'].includes(e.key.toLowerCase())) {{
      e.preventDefault();
    }}
  }});
}}
</script>
</body>
</html>"""


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
