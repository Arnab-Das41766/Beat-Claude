---
description: Start the Beat Claude stack (Local LLM + Tunnel + Cloud Backend)
---

Follow these steps to get the hybrid cloud/local system running.

### 1. Start Local LLM (Ollama)
Ensure Ollama is running on your machine.
// turbo
1. Run `ollama list` to verify.

### 2. Start Local Backend
// turbo
1. Open a terminal in `backend/` and run `python main.py`.
2. Keep this terminal open.

### 3. Start Cloudflare Tunnel
// turbo
1. Open a new terminal and run `cloudflared tunnel --url http://localhost:8000`.
2. Copy the `https://*.trycloudflare.com` URL.

### 4. Link Railway to Tunnel
1. Go to [Railway Dashboard](https://railway.app/).
2. Select the `Beat-Claude` project.
3. Under **Variables**, update `LOCAL_BACKEND_URL` with your tunnel URL.
4. Deployment will trigger automatically.

### 5. Access Frontend
Visit [beat-claude.vercel.app](https://beat-claude.vercel.app/) to use the app.
