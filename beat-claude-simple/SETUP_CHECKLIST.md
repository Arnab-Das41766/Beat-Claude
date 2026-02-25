# Beat Claude — Full Startup & Verification Checklist

> **Architecture:** Vercel (frontend) → Supabase (auth) → Railway (cloud backend) → Cloudflare Tunnel → Local PC (LLM backend)

---

## Step 1: Check Vercel (Frontend)

**What:** Recruiter frontend (HTML pages — `auth.html`, `dashboard.html`, `create-exam.html`, `results.html`)

- [ ] Go to [Vercel Dashboard](https://vercel.com/dashboard)
- [ ] Confirm your project shows **"Ready"** status
- [ ] Visit your Vercel URL → should load the **Beat Claude** landing page
- [ ] If not working → click **Redeploy** from the latest commit

**Verify:** Open the Vercel URL in browser — you should see the Beat Claude homepage

---

## Step 2: Check Supabase (Auth & Database)

**What:** Handles recruiter sign-up / sign-in

- [ ] Go to [Supabase Dashboard](https://supabase.com/dashboard)
- [ ] Find project **`lvohxrnftwfxfudvnozm`**
- [ ] Make sure it says **Active** (not paused — free tier pauses after 1 week of inactivity!)
- [ ] If paused → click **"Restore Project"** and wait ~2 minutes

**Verify:** Visit `https://lvohxrnftwfxfudvnozm.supabase.co` in browser — should NOT be an error page

### If credentials changed:
- Go to **Settings → API** in Supabase Dashboard
- Copy the new **anon key** and update these 4 frontend files:
  - `recruiter_frontend/auth.html`
  - `recruiter_frontend/dashboard.html`
  - `recruiter_frontend/create-exam.html`
  - `recruiter_frontend/results.html`

---

## Step 3: Check Railway (Cloud Backend)

**What:** Cloud backend at `https://beat-claude-production.up.railway.app` — handles exam creation, candidate submissions, results

- [ ] Go to [Railway Dashboard](https://railway.app/dashboard)
- [ ] Find the **Beat Claude** project
- [ ] Check the service is **deployed and running** (green status)
- [ ] If crashed → check **Deploy Logs** for errors

**Verify:** Visit `https://beat-claude-production.up.railway.app/` — should return:
```json
{"status": "ok", "message": "Beat Claude Cloud Backend", "docs": "/docs"}
```

---

## Step 4: Start Local Backend (LLM on your PC)

**What:** Your local `backend/main.py` — runs Ollama AI for question generation & scoring

- [ ] Make sure **Ollama** is running (check System Tray or run `ollama list`)
- [ ] Open terminal in `beat-claude-simple/backend/`
- [ ] Run:
  ```
  python main.py
  ```
- [ ] Should start on `http://localhost:8000`

**Verify:** Visit `http://localhost:8000/docs` in browser — should show FastAPI docs

---

## Step 5: Start Cloudflare Tunnel (Bridge Railway → Local PC)

**What:** Exposes your local `localhost:8000` to the internet so Railway can reach your LLM

- [ ] Run Cloudflare Tunnel:
  ```
  cloudflared tunnel --url http://localhost:8000
  ```
- [ ] Copy the generated URL (looks like `https://xxxxx.trycloudflare.com`)

**Verify:** Open the Cloudflare URL in browser — should show same response as `localhost:8000`

---

## Step 6: Update Railway's `LOCAL_BACKEND_URL` (CRITICAL!)

**What:** Tell Railway where to find your local LLM backend

- [ ] Go to [Railway Dashboard](https://railway.app/dashboard) → your project → **Variables**
- [ ] Set `LOCAL_BACKEND_URL` = your new Cloudflare tunnel URL (e.g. `https://xxxxx.trycloudflare.com`)
- [ ] **Redeploy** the service (Railway auto-redeploys on variable change)

**Verify:** The `/api/create-exam` endpoint on Railway should now be able to reach your local LLM

---

## Full End-to-End Test

Once all 6 steps are done, test the complete flow:

1. [ ] Open the Vercel frontend URL
2. [ ] Click **Sign In** → sign up or log in via Supabase auth
3. [ ] Go to **Dashboard** → click **Create Exam**
4. [ ] Paste a job description and submit → Railway calls your local LLM via Cloudflare
5. [ ] If exam gets created with questions → **everything is working!** 🎉

---

## Quick Troubleshooting

| Problem | Likely Cause |
|---|---|
| "Failed to fetch" on auth page | Supabase project is **paused** — restore it |
| Auth works but dashboard is empty | Railway is down or `API_BASE` URL is wrong |
| "Local backend is offline" error | Cloudflare tunnel not running OR `LOCAL_BACKEND_URL` not updated on Railway |
| Exam creation hangs forever | Ollama not running on local PC, or model not loaded |
| Cloudflare URL changed | You restarted the tunnel — update Railway's `LOCAL_BACKEND_URL` again |
