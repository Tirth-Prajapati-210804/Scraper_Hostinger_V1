# Hostinger VPS Deployment

This project is VPS-ready with Docker Compose. The simplest path is:

- keep the frontend on Vercel and move only the backend + database to the VPS, or
- run the full stack with Docker Compose on the VPS

For this repo, Docker Compose is the safest path. Production collection is ScrapingBee-only and ScrapingBee scrapes live KAYAK result pages.

## Recommended VPS size

Minimum practical size for this scraper:

- `4 GB RAM` if usage is light
- `8 GB RAM` recommended for multi-city ScrapingBee runs

## Files used

- `docker-compose.yml`
- `docker-compose.hostinger.yml`
- `backend/Dockerfile`
- `frontend/Dockerfile`
- `frontend/nginx.conf`
- `backend/.env`
- `.env.hostinger`

## 1. Prepare the VPS

SSH into the server and install Docker + Compose plugin.

Ubuntu/Debian example:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Optional but recommended:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

## 2. Upload the repo

```bash
git clone https://github.com/Tirth-Prajapati-210804/Sc-new-free-Scraper.git
cd Sc-new-free-Scraper
```

## 3. Create env files

Backend app env:

```bash
cp backend/.env.example backend/.env
```

Hostinger/VPS overlay env:

```bash
cp .env.hostinger.example .env.hostinger
```

Set these values before starting:

- `DB_PASSWORD`
- `JWT_SECRET_KEY`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `SCRAPINGBEE_API_KEY`
- `CORS_ORIGINS`
- `ALLOWED_HOSTS`
- `PROVIDER_CONCURRENCY_LIMIT`

Important:

- ScrapingBee is the only production provider.
- The scraper collects from KAYAK pages through ScrapingBee.
- Use only one ScrapingBee key source if possible.
- Prefer `SCRAPINGBEE_API_KEY` for a single key.
- Leave `SCRAPINGBEE_API_KEYS` empty unless you intentionally want a key pool.
- Start with `PROVIDER_CONCURRENCY_LIMIT=2`; increase only after a stable live scrape run.

## 4. Start the stack

```bash
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml up -d --build
```

This starts:

- Postgres
- FastAPI backend
- React frontend served by nginx

## 5. Check health

```bash
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml ps
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml logs backend --tail=200
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml logs frontend --tail=100
curl http://localhost/health
curl http://localhost/health/ready
```

## 6. Daily management

You do not need to manage it manually every day if setup is stable.

Typical commands:

```bash
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml ps
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml logs -f backend
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml restart backend
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml up -d --build
git pull
```

For code updates:

```bash
git pull
docker compose --env-file .env.hostinger -f docker-compose.yml -f docker-compose.hostinger.yml up -d --build
```

## 7. SSL / domain

Best practical setup:

- point your domain/subdomain to the VPS
- put Cloudflare in front, or
- use Hostinger DNS + reverse proxy SSL if available

If you want HTTPS directly on the VPS, the clean next step is to add Caddy or Nginx Proxy Manager in front of the frontend/backend containers.

## 8. Suggested deployment model

Best low-cost model for this project:

- frontend stays on Vercel
- backend + database move to Hostinger VPS

Why:

- easier frontend deploys
- backend gets proper RAM
- scheduler and scraping become more stable

## 9. Important operational notes

- Render free instance OOM was a hosting limitation, not only an app bug.
- Multi-city scraping is memory heavier than one-way or round-trip.
- Keep provider concurrency conservative on first VPS deployment.
- Keep the app running with Docker restart policies; if the VPS or container restarts, the backend scheduler starts again automatically.
- After migration, test:
  - login
  - route group creation
  - trigger scrape
  - export
  - scheduler run

## 10. Recommended first live settings on VPS

- `PROVIDER_TIMEOUT_SECONDS=60`
- `PROVIDER_MAX_RETRIES=1`
- `PROVIDER_CONCURRENCY_LIMIT=2`
- `PROVIDER_MIN_DELAY_SECONDS=0.5`
- `SCRAPINGBEE_PREMIUM_PROXY=false`
- `SCRAPINGBEE_STEALTH_PROXY=false`
