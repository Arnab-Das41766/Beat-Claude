# Beat Claude - AI Hiring Companion

A complete, production-ready AI Hiring SaaS platform that runs entirely locally with no paid APIs. Built with vanilla HTML/CSS/JS frontend and Python FastAPI backend.

![Beat Claude](https://img.shields.io/badge/Beat-Claude-violet)
![Python](https://img.shields.io/badge/Python-3.8+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Architecture Overview

Beat Claude uses a **split architecture** with two FastAPI backends:

```
┌────────────────────────────────────┐       ┌──────────────────────────────────┐
│     LOCAL BACKEND (localhost:8000)  │       │  CLOUD BACKEND (Railway/Render)  │
│                                    │       │                                  │
│  ▸ Recruiter dashboard & auth      │  HTTP │  ▸ Shareable exam links          │
│  ▸ AI question generation (Ollama) │◄─────►│  ▸ Candidate-facing exam page    │
│  ▸ AI open-ended grading           │       │  ▸ MCQ auto-grading              │
│  ▸ Full recruiter workflow         │       │  ▸ SQLite database               │
│                                    │       │  ▸ Results API for recruiters     │
│  Exposed via Cloudflare Tunnel     │       │  Deployed on Railway (free tier)  │
└────────────────────────────────────┘       └──────────────────────────────────┘
         Your Machine                                  Cloud
```

**How the flow works:**
1. Recruiter pastes a job description → local backend generates MCQ questions via local LLM
2. Cloud backend creates a shareable exam link (e.g., `https://yourapp.railway.app/exam/aB3x9kM2pQ7z`)
3. Candidate opens the link, fills info, takes the test
4. MCQs are auto-graded instantly on the cloud; open-ended answers are graded by the local LLM
5. Recruiter views results on the cloud backend

---

## Quick Start

### Prerequisites

1. **Python 3.8+** — [Download](https://python.org)
2. **Ollama** — [Download](https://ollama.com/download)
3. **cloudflared** — [Download](https://github.com/cloudflare/cloudflared/releases) (for exposing local backend)

### Step 1: Clone the Repository

```bash
git clone <your-repo-url>
cd beat-claude-simple
```

### Step 2: Set Up the Local Backend

```bash
cd backend
pip install -r requirements.txt

# Copy and edit environment variables
# (already pre-configured for local development)
# Edit .env to change OLLAMA_MODEL, SECRET_KEY, etc.

# Pull the AI model (first time only)
ollama pull qwen2.5-coder:7b

# Start Ollama (in a separate terminal)
ollama serve

# Start the local backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — the recruiter dashboard is now running.

### Step 3: Set Up Cloudflare Tunnel

This exposes your local backend to the internet so the cloud backend can reach it.

#### Option A: Quick Tunnel (temporary URL, changes each time)

```bash
cloudflared tunnel --url http://localhost:8000
```

Copy the generated URL (e.g., `https://something-random.trycloudflare.com`).

#### Option B: Permanent Tunnel (requires Cloudflare account)

```bash
# Login to Cloudflare
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create beat-claude

# Create config file at ~/.cloudflared/config.yml
# tunnel: <tunnel-id>
# credentials-file: /path/to/.cloudflared/<tunnel-id>.json
# ingress:
#   - hostname: api.yourdomain.com
#     service: http://localhost:8000
#   - service: http_status:404

# Route DNS
cloudflared tunnel route dns beat-claude api.yourdomain.com

# Run the tunnel
cloudflared tunnel run beat-claude
```

### Step 4: Set Up the Cloud Backend

```bash
cd cloud_backend
pip install -r requirements.txt

# Copy .env.example to .env and fill in:
cp .env.example .env
```

Edit `cloud_backend/.env`:
```env
INTERNAL_API_KEY=beat-claude-internal-key-2024    # Must match backend/.env
LOCAL_BACKEND_URL=https://your-tunnel-url.com      # From step 3
DATABASE_URL=sqlite:///./exams.db
CLOUD_APP_URL=http://localhost:9000                # Or your Railway URL
```

```bash
# Start the cloud backend locally
uvicorn main:app --host 0.0.0.0 --port 9000
```

### Step 5: Deploy Cloud Backend to Railway

1. Push the `cloud_backend/` folder to a GitHub repo
2. Go to [Railway](https://railway.app) → New Project → Deploy from GitHub
3. Set the root directory to `cloud_backend`
4. Add environment variables:
   - `INTERNAL_API_KEY` — same as in `backend/.env`
   - `LOCAL_BACKEND_URL` — your Cloudflare tunnel URL
   - `CLOUD_APP_URL` — Railway will provide this (e.g., `https://yourapp.up.railway.app`)
   - `DATABASE_URL` — `sqlite:///./exams.db`
5. Railway will auto-detect the `Procfile` and deploy!

### Step 6: Test the Full Flow

```bash
# 1. Create an exam (replace URLs and keys with your actual values)
curl -X POST https://your-cloud-url/api/create-exam \
  -H "Content-Type: application/json" \
  -d '{"job_description": "We need a Python developer with 3+ years of experience in FastAPI, REST APIs, and SQL databases.", "recruiter_email": "recruiter@example.com"}'

# Response:
# { "slug": "aB3x9kM2pQ7z", "exam_link": "https://your-cloud-url/exam/aB3x9kM2pQ7z", ... }

# 2. Open the exam link in a browser — candidate takes the test

# 3. View results
curl https://your-cloud-url/recruiter/results/aB3x9kM2pQ7z
```

---

## Project Structure

```
beat-claude-simple/
├── backend/                    # LOCAL BACKEND (recruiter + AI)
│   ├── main.py                 # FastAPI app (recruiter dashboard + internal API)
│   ├── requirements.txt
│   ├── .env
│   └── beat_claude.db          # Recruiter database
├── cloud_backend/              # CLOUD BACKEND (candidate-facing)
│   ├── main.py                 # FastAPI app with exam pages
│   ├── requirements.txt
│   ├── .env / .env.example
│   ├── Procfile                # Railway/Render deployment
│   ├── railway.json            # Railway config
│   └── exams.db                # Exam database (auto-created)
├── frontend/                   # Recruiter frontend (served by local backend)
│   ├── css/style.css
│   ├── js/app.js
│   ├── index.html
│   └── pages/                  # 11 HTML pages
├── setup.py
├── start.bat / start.sh
├── CLOUDFLARE_TUNNEL.md
└── README.md
```

---

## API Reference

### Local Backend (localhost:8000)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/generate-exam` | POST | `X-Internal-Key` | Generate MCQ questions from job description |
| `/grade-open-ended` | POST | `X-Internal-Key` | AI-grade an open-ended answer |
| `/api/assessments` | POST | Session | Create assessment (recruiter dashboard) |
| `/api/login` | POST | — | Recruiter login |
| _...and 15+ more_ | | | Full recruiter dashboard API |

### Cloud Backend (Railway)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/create-exam` | POST | Create exam → generates shareable link |
| `/exam/{slug}` | GET | Candidate exam page (HTML) |
| `/exam/{slug}/submit` | POST | Submit answers, auto-grade MCQs |
| `/recruiter/results/{slug}` | GET | Full results for an exam |

---

## Environment Variables

### Local Backend (`backend/.env`)

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Session encryption key | `change-this-secret-key-in-production` |
| `OLLAMA_URL` | Ollama API URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | LLM model name | `mistral:7b-instruct-q4_K_M` |
| `DB_PATH` | SQLite database path | `beat_claude.db` |
| `INTERNAL_API_KEY` | Shared secret with cloud backend | (required) |

### Cloud Backend (`cloud_backend/.env`)

| Variable | Description | Default |
|----------|-------------|---------|
| `INTERNAL_API_KEY` | Must match local backend | (required) |
| `LOCAL_BACKEND_URL` | Cloudflare tunnel URL | `http://localhost:8000` |
| `DATABASE_URL` | SQLite connection string | `sqlite:///./exams.db` |
| `CLOUD_APP_URL` | Public URL of cloud backend | `http://localhost:9000` |

---

## Features

### Recruiter Dashboard (Local)
- Register/login with session-based auth
- Paste job description → AI generates assessment
- Publish/close assessments
- View results, leaderboard, candidate details
- AI scoring with strengths/gaps analysis
- CSV export

### Candidate Exam (Cloud)
- Clean, professional single-page exam experience
- Shareable links that work from anywhere
- MCQ auto-grading (instant)
- Open-ended AI grading (via local LLM)
- Anti-cheat: tab switch detection, auto-submit after 3 violations
- Right-click, copy/paste disabled during exam
- Question order randomized per candidate (seeded by email)
- Timer with visual warning at 5 minutes remaining
- Graceful degradation: if local LLM is offline, MCQs still grade, open-ended marked "pending"

---

## Troubleshooting

### Ollama Connection Issues
```bash
curl http://localhost:11434/api/tags  # Check if Ollama is running
ollama serve                          # Restart Ollama
ollama pull qwen2.5-coder:7b         # Re-pull model
```

### Cloud Backend Can't Reach Local
- Verify Cloudflare tunnel is running
- Check `LOCAL_BACKEND_URL` in cloud `.env` matches tunnel URL
- Check `INTERNAL_API_KEY` matches in both `.env` files

### Exam Links Not Working
- Verify cloud backend is running
- Check `CLOUD_APP_URL` matches actual deployment URL

---

## License

MIT License — Free for personal and commercial use.

---

**Beat Claude** — Making hiring smarter with local AI 🚀
