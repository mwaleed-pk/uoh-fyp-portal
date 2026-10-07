<div align="center">

# 🎓 FYP Portal — University of Haripur

### Final Year Project Management, End-to-End

**From proposal to defense: one portal for students, supervisors and admins to run the entire FYP lifecycle.**

[![React](https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-8-646CFF?style=for-the-badge&logo=vite&logoColor=white)](https://vite.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Supabase](https://img.shields.io/badge/Supabase-PostgreSQL-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)](https://supabase.com/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)

</div>

---

## 📖 Overview

The **FYP Portal** digitizes how the University of Haripur runs Final Year Projects. Students submit proposals and documents, supervisors review and schedule meetings, and admins govern the whole pipeline — projects, reviews, announcements, notifications and user taxonomy — from a single workspace.

It is a true full-stack system: a **React + Vite** frontend paired with a cleanly-layered **FastAPI** backend (routes → services → Supabase), with structured logging, rate limiting, background tasks and a test suite.

---

## ✨ Features

- **🧑‍🎓 Student workspace** — project proposals, document uploads, progress tracking and meeting schedules
- **🧑‍🏫 Supervisor workspace** — review queues, feedback, project supervision and student coordination
- **🛡️ Admin workspace** — user management, project oversight, taxonomy and platform announcements
- **📄 Document pipeline** — upload, versioning and review of FYP documents (PDF tooling via `pdfjs-dist`)
- **⭐ Review & evaluation flows** — structured review rounds with dedicated routes and services
- **📅 Meetings module** — schedule and track supervisor–student meetings
- **🔔 Notifications engine** — in-app notifications plus email delivery (SMTP) with background workers
- **📣 Announcements** — broadcast updates to students and supervisors
- **⚡ Production-grade backend** — rate limiting (slowapi), structured logs (structlog), Celery + Redis task queue, pytest suite

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, React Router 7 |
| UI | Lucide icons, react-hot-toast, date-fns |
| PDF | pdfjs-dist |
| HTTP | axios, Supabase JS client |
| Backend | FastAPI, Uvicorn, Pydantic v2, Pydantic Settings |
| Auth | python-jose (JWT), passlib/bcrypt |
| Data | Supabase (PostgreSQL), openpyxl (exports) |
| Async | Celery + Redis, aiosmtplib, httpx |
| Quality | pytest, pytest-asyncio, oxlint, structlog, slowapi |
| Deployment | Netlify (frontend SPA), Uvicorn (backend) |

---

## 🏗️ Architecture

```
React SPA (Vite)
    │  axios / Supabase client
    ▼
FastAPI (backend/)
    ├── routes/      auth, users, projects, documents, reviews,
    │                meetings, notifications, announcements,
    │                supervisor, admin, taxonomy, public, health
    ├── services/    auth_service, project_service, notification_service
    ├── models/      Pydantic schemas
    ├── db/          Supabase client
    ├── email/       Jinja2 templates + SMTP
    └── utils/       config, logging
    ▼
Supabase PostgreSQL (+ Auth)
    ▼
Celery + Redis ──► background jobs (emails, notifications)
```

`main.py` is a single deployable entry point: bootstrap, router registration and middleware only — all business logic lives in `app/services/`, all handlers in `app/routes/`.

---

## 🚀 Getting Started

### Prerequisites

- Node.js 22+ and Python 3.11+
- A Supabase project
- Redis (for Celery background tasks)

### Frontend

```bash
git clone https://github.com/mwaleed-pk/fyp-portal-for-uoh.git
cd fyp-portal-for-uoh
npm install
npm run dev
```

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create `backend/.env` with your own values (**never commit real secrets**):

```env
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_KEY="your-anon-key"
SUPABASE_SERVICE_ROLE_KEY="your-service-role-key"
JWT_SECRET_KEY="generate-a-long-random-secret"
SMTP_HOST="your-smtp-host"
SMTP_USER="your-smtp-user"
SMTP_PASSWORD="your-smtp-password"
REDIS_URL="redis://localhost:6379/0"
```

```bash
uvicorn main:app --reload          # API on http://localhost:8000
celery -A app.celery worker -l info  # background worker
pytest                               # run tests
```

---

## 📁 Project Structure

```
├── backend/
│   ├── main.py                  # app entry: bootstrap, routers, middleware
│   ├── requirements.txt
│   ├── app/
│   │   ├── routes/              # auth, projects, documents, reviews, meetings,
│   │   │                        # notifications, announcements, supervisor, admin…
│   │   ├── services/            # business logic (auth, projects, notifications)
│   │   ├── models/              # Pydantic schemas
│   │   ├── db/                  # Supabase client
│   │   ├── email/               # templates + SMTP delivery
│   │   └── utils/               # config, logging setup
│   └── tests/
├── index.html                   # "FYP UOH"
├── netlify.toml / _redirects     # SPA build + routing
├── uoh-logo.png / uoh-campus.jpg # brand assets
└── vite.config.js
```

---

## 🗺️ Roadmap

- [ ] Plagiarism/AI-similarity report integration
- [ ] Defense scheduling with panel management
- [ ] External examiner portal
- [ ] Analytics dashboard (department-level FYP stats)
- [ ] Mobile-responsive supervisor approvals

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Run `pytest` and `npm run lint` before committing
4. Open a Pull Request with a clear description

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for details.

---

## 👤 Author

**Muhammad Waleed (MW Trader)** — [github.com/mwaleed-pk](https://github.com/mwaleed-pk)
