# Beat Claude — Full Startup & Verification Checklist

> **Architecture:** Vercel (Frontend) → Railway (Cloud Backend + JWT Auth) → Cloudflare Tunnel → Local PC (LLM Backend/Ollama)

---

## Step 1: Check Vercel (Frontend)

- [ ] Visit [beat-claude.vercel.app](https://beat-claude.vercel.app/)
- [ ] Ensure the landing page loads correctly.
- [ ] **Auth Check:** Navigate to `auth.html`. It should load instantly with no errors.

---

## Step 2: Start Local Backend (LLM on your PC)

**What:** `backend/main.py` — runs Ollama for question generation.
- [ ] Ensure **Ollama** is running (`ollama list`).
- [ ] In terminal: `cd backend && python main.py`.
- [ ] **Health Check:** Visit `http://localhost:8000/docs`.

---

## Step 3: Start Cloudflare Tunnel

**What:** Tunnel from `localhost:8000` to the internet.
- [ ] In terminal: `cloudflared tunnel --url http://localhost:8000`.
- [ ] **Copy the URL** (e.g., `https://xxxxx.trycloudflare.com`).

---

## Step 4: Update Railway Configuration

**What:** Update Cloud Backend at `https://beat-claude-production.up.railway.app`.

1. Go to [Railway Variables](https://railway.app/dashboard)
2. Update **`LOCAL_BACKEND_URL`** to the new Cloudflare URL.
3. Ensure **`JWT_SECRET`** is set (persists user logins).
4. Ensure **`INTERNAL_API_KEY`** matches your local `.env`.
5. **Redeploy** to apply changes.

---

## Step 5: Sync Local API Key

- [ ] Check `backend/.env` for `INTERNAL_API_KEY`.
- [ ] Must match the `INTERNAL_API_KEY` in Railway dashboard.

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---|---|---|
| 503 Service Unavailable | Railway can't reach tunnel | Update `LOCAL_BACKEND_URL` in Railway |
| 403 Forbidden | API Key mismatch | Ensure `INTERNAL_API_KEY` matches in `.env` and Railway |
| Users log out often | Missing `JWT_SECRET` | Set a static `JWT_SECRET` in Railway Variables |
| LLM hangs | Ollama speed | Check Ollama logs or try a smaller model |

