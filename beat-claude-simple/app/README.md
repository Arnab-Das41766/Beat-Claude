# Beat Claude — AI Hiring Platform (v2 — Groq Edition)

A streamlined AI hiring assessment platform. **Single backend, single frontend, zero local AI model required.**

## Architecture

```
app/
├── backend/
│   ├── main.py          ← Unified FastAPI (JWT auth + Groq AI + SQLite + serves frontend)
│   ├── requirements.txt
│   ├── .env             ← Your secrets (copy from .env.example)
│   └── .env.example
└── frontend/
    ├── index.html       ← Landing page
    ├── auth.html        ← Recruiter login / signup
    ├── dashboard.html   ← Recruiter dashboard (list exams)
    ├── create-exam.html ← Create a new exam
    └── results.html     ← View candidate results
```

## Run locally

### Prerequisites
- Python 3.9+
- A **free Groq API key** → [console.groq.com/keys](https://console.groq.com/keys)

### Steps

```bash
cd beat-claude-simple/app/backend

# Install dependencies
pip install -r requirements.txt

# Copy and fill your .env
copy .env.example .env
# Open .env and paste your GROQ_API_KEY

# Start the server
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** in your browser — the full app is running!

## How it works

1. **Sign up** as a recruiter at `/auth.html`
2. Go to **Dashboard** → **Create New Exam**
3. Paste a job description → Groq AI instantly generates questions
4. Copy the **exam link** and send it to candidates
5. Candidates complete the timed exam in their browser
6. Return to **Results** to see AI-graded scores and feedback

## Deploy to Railway / Render

1. Push the `app/` folder to GitHub
2. Create a new service pointing to the `app/backend` directory
3. Set environment variables:
   - `GROQ_API_KEY` ← your Groq key
   - `JWT_SECRET` ← any long random string
   - `APP_URL` ← your Railway/Render app URL (e.g., `https://yourapp.up.railway.app`)
4. The start command is: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | ✅ | Groq API key for AI question generation + grading |
| `JWT_SECRET` | ✅ in prod | Secret for JWT token signing |
| `APP_URL` | ✅ in prod | Public URL for exam links (e.g., `https://yourapp.railway.app`) |
| `GROQ_MODEL` | optional | Groq model name (default: `llama-3.1-8b-instant`) |
| `DB_PATH` | optional | SQLite file path (default: `beat_claude.db`) |

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup` | — | Register a recruiter |
| POST | `/auth/signin` | — | Login, get JWT |
| GET | `/auth/me` | JWT | Verify token |
| POST | `/api/create-exam` | JWT | Generate exam from job description |
| GET | `/recruiter/exams?email=` | — | List recruiter's exams |
| GET | `/recruiter/results/{slug}` | — | Full results for an exam |
| GET | `/exam/{slug}` | — | Candidate exam page (HTML) |
| POST | `/exam/{slug}/submit` | — | Submit candidate answers |
