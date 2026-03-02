# Render + Supabase Setup (Local Frontend, Remote Backend)

This guide sets up the exact flow you asked for:

- Frontend runs on your laptop (`localhost:5173`)
- Backend runs on Render
- Run history and state are stored in Supabase

---

## 1) Prerequisites

- Supabase project ready
- Render account ready
- Repo pushed to GitHub (`develop-2` branch)
- Google/Supabase auth already working locally

---

## 2) Supabase database setup

Run this migration in Supabase SQL Editor:

- `db/migrations/001_user_and_chat_runs.sql`

This creates:

- `public.app_users`
- `public.chat_runs`

and read policies for authenticated users.

---

## 3) Deploy backend to Render

You can use Blueprint (`render.yaml`) or manual setup.

### Option A: Blueprint (recommended)

1. In Render, choose **New +** -> **Blueprint**
2. Select this repo and branch `develop-2`
3. Confirm service from `render.yaml`
4. Set secrets (below) in Render dashboard
5. Deploy

### Option B: Manual web service

- Runtime: Python
- Build Command: `pip install --upgrade pip && pip install .`
- Start Command: `uvicorn api_server:app --host 0.0.0.0 --port $PORT`
- Health check path: `/api/health`

---

## 4) Render backend environment variables

Set these in the Render service:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_SECRET`
- `SUPABASE_JWT_AUDIENCE=authenticated`
- `SUPABASE_JWT_ISSUER` (optional; if omitted, backend derives it from `SUPABASE_URL`)
- `GEMINI_API_KEY` (or whichever text model key your config uses)
- `LOCAL_RUN_STORE=0`
- `SAVE_LOCAL_SESSIONS=0`
- `RUN_TIMEOUT_SEC=900`
- `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`

Notes:

- `LOCAL_RUN_STORE=0` forces Supabase-backed run persistence.
- `SAVE_LOCAL_SESSIONS=0` avoids relying on Render's ephemeral filesystem.
- Add your future Vercel domain to `CORS_ORIGINS` when needed.

---

## 5) Run frontend locally against Render backend

Because `frontend/vite.config.js` uses `envDir: ".."`, place frontend env vars in the project root.

Create/update root `.env.local`:

```env
VITE_SUPABASE_URL=your_supabase_url
VITE_SUPABASE_ANON_KEY=your_supabase_anon_key
VITE_API_BASE_URL=https://your-render-service.onrender.com
```

Then start frontend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

---

## 6) Smoke-test checklist

1. Backend health:
   - `GET https://<render>/api/health` -> `{ "status": "ok" }`
2. Login from local frontend (Supabase auth)
3. Create a run
4. Confirm run appears in UI history
5. Refresh page and confirm run persists (loaded from Supabase)
6. Confirm SSE updates stream while run is active

---

## 7) Common issues and fixes

- **401 Unauthorized**
  - Verify token is sent from frontend.
  - Recheck `SUPABASE_JWT_SECRET`, `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`.

- **CORS errors**
  - Ensure local origins are included in `CORS_ORIGINS`.
  - Use exact scheme + host + port.

- **Runs not persisting**
  - Verify `LOCAL_RUN_STORE=0`.
  - Verify `SUPABASE_SERVICE_ROLE_KEY` is set.
  - Check Render logs for `/rest/v1/chat_runs` errors.

- **Frontend still calling localhost backend**
  - Ensure `VITE_API_BASE_URL` is set in root `.env.local`.
  - Restart Vite after changing env vars.

---

## 8) Recommended branch workflow

- Debug hosting and infra on `develop-2`
- Deploy `develop-2` first
- Once stable, merge/cherry-pick to `main`
- Keep `main` as production-only branch
