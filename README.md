# Flight Harvester / Scraper-v4

Flight Harvester is a full-stack flight-price collection system with a FastAPI backend, a React + Vite frontend, and PostgreSQL persistence. It supports JWT authentication, scheduled collection, manual collection triggers, historical price views, logs, Excel export, round-trip groups, and multi-city groups.

The production provider is ScrapingBee scraping live KAYAK result pages.

## Stack

Backend: FastAPI, SQLAlchemy 2.x async, Alembic, APScheduler.
Frontend: React, TypeScript, Vite, Tailwind, React Query.
Storage: PostgreSQL.
Deployment: Hostinger VPS Docker Compose is the canonical production path. Legacy Render and Railway files remain in the repo for reference only and are not the active deployment story.

## VPS / Hostinger readiness

This repo is already deployable on a VPS with Docker Compose.

Files prepared for that path:

- `docker-compose.yml`
- `docker-compose.hostinger.yml`
- `backend/Dockerfile`
- `frontend/Dockerfile`
- `.env.hostinger.example`
- `docs/HOSTINGER_VPS.md`

Canonical production deployment:

- full stack on the Hostinger VPS with Docker Compose
- Cloudflare in front once you have a stable domain

For step-by-step VPS deployment, see:

- `docs/HOSTINGER_VPS.md`

## Repository layout

```text
flight-harvester/
|-- backend/
|   |-- app/
|   |-- alembic/
|   |-- tests/
|   |-- Dockerfile
|   `-- pyproject.toml
|-- frontend/
|   |-- src/
|   |-- e2e/
|   |-- Dockerfile
|   `-- package.json
|-- .github/workflows/ci.yml
|-- docker-compose.yml
`-- render.yaml
```

## Local setup

### Backend

```bash
cd backend
cp .env.example .env
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

The backend requires PostgreSQL before startup. For local development, either run your own Postgres instance on `localhost:5432` or start the Compose database service first:

```bash
docker compose up -d db
cd backend
.venv/bin/python -m alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend expects `VITE_API_BASE_URL` to point at the backend in local development.

## Required environment variables

### Backend

`DATABASE_URL` - must use `postgresql+asyncpg://...` and include `sslmode=require` for Supabase.

`JWT_SECRET_KEY` - at least 32 characters.

`ADMIN_EMAIL` / `ADMIN_PASSWORD` - bootstrap admin account.

`SCRAPINGBEE_API_KEY` / `SCRAPINGBEE_API_KEYS` - enables the primary real provider. Use one key in `SCRAPINGBEE_API_KEY` or a comma-separated / JSON array pool in `SCRAPINGBEE_API_KEYS`.

`SCRAPINGBEE_COUNTRY_CODE` - default country or market used for KAYAK rendering through ScrapingBee. Default `us`.

`SCRAPINGBEE_PREMIUM_PROXY`, `SCRAPINGBEE_STEALTH_PROXY` - optional higher-cost proxy modes for tougher routes or higher anti-bot pressure.

`CORS_ORIGINS` - explicit allowed browser origins.

`ALLOWED_HOSTS` - trusted host allow-list.

`SCHEDULER_ENABLED`, `SCHEDULER_INTERVAL_MINUTES` - collection cadence.

`SCRAPE_BATCH_SIZE`, `SCRAPE_ROUTE_PARALLELISM`, `SCRAPE_DELAY_SECONDS`, `PROVIDER_TIMEOUT_SECONDS`, `PROVIDER_MAX_RETRIES`, `PROVIDER_CONCURRENCY_LIMIT`, `PROVIDER_MIN_DELAY_SECONDS` - collection tuning.

`LOGIN_RATE_LIMIT_ATTEMPTS`, `LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `SCRAPE_RATE_LIMIT_ATTEMPTS`, `SCRAPE_RATE_LIMIT_WINDOW_SECONDS` - rate limits.

`SENTRY_DSN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - optional monitoring hooks.

### Frontend

`VITE_API_BASE_URL` - backend base URL for local development or for the deployed frontend.

## Production deployment

### Hostinger VPS Docker Compose

Deploy the full stack with:

```bash
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml up -d --build
```

Use `docs/HOSTINGER_VPS.md` as the source of truth for:

- required env vars
- Docker Compose commands
- migration commands
- Cloudflare/domain guidance
- first-run verification

### Legacy configs

`render.yaml` and `backend/railway.toml` are legacy references only. They are not the maintained production path and should not be treated as current deployment documentation.

## Health checks

`GET /health` - overall health summary.

`GET /health/live` - liveness.

`GET /health/ready` - readiness for deployment checks.

## Client handoff notes

The API is protected with JWT bearer tokens, login and scrape trigger rate limiting, and redacted structured logging. All authenticated users share the same tracker data and can view, create, edit, and trigger collections. Only user management is admin-only, and route-group delete is admin-only. The scheduler uses a PostgreSQL advisory lock to prevent duplicate collection runs, fully scraped groups are skipped automatically, and multi-city special legs are collected end to end.

## Testing

Backend unit tests:

```bash
cd backend
python -m pytest tests/test_airline_codes.py tests/test_auth_schema.py tests/test_config.py tests/test_route_group_schema.py tests/test_services
```

Frontend build:

```bash
cd frontend
npm run build
```

Live provider smoke test:

```bash
cd backend
python -m scripts.verify_scrapingbee
```

Full verification used for client handoff:

```bash
cd backend
python -m pytest

cd ../frontend
npm run lint
npm run test:run
npm run build
```

Live deploy smoke test:

```bash
cd frontend
PLAYWRIGHT_LIVE_BASE_URL=https://your-live-url \
PLAYWRIGHT_LIVE_EMAIL=admin@example.com \
PLAYWRIGHT_LIVE_PASSWORD=your-password \
npm run e2e:live
```

## Troubleshooting

If login fails, verify `JWT_SECRET_KEY`, `ADMIN_EMAIL`, and `ADMIN_PASSWORD`.

If the backend fails on startup with a PostgreSQL connection error, confirm a database is running on `localhost:5432` for host-based development, or start Docker Desktop and run `docker compose up -d db`.

If collection is disabled or provider status looks degraded, confirm `SCRAPINGBEE_API_KEY` or `SCRAPINGBEE_API_KEYS` is set, make sure the ScrapingBee account still has credits available, and keep provider concurrency low enough to avoid `429` concurrent-request errors.

If the frontend cannot reach the API, verify `VITE_API_BASE_URL`, `CORS_ORIGINS`, and the deployed Render backend URL.

If Render health checks fail, confirm the backend can connect to Supabase with `sslmode=require` and that `SCHEDULER_ENABLED` is true.
