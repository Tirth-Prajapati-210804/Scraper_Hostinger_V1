FROM node:24-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --include=dev --no-audit --no-fund \
    && test -x node_modules/.bin/tsc \
    && test -x node_modules/.bin/vite

COPY . .

# In Docker the API is proxied by nginx (location /api/ -> backend:8000),
# so VITE_API_BASE_URL must be empty to use relative URLs.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80 443

# HTTP now 301-redirects to HTTPS, so probe HTTPS directly. --no-check-certificate
# because the origin serves a Cloudflare Origin cert (not browser-trusted, by design).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget --quiet --tries=1 --no-check-certificate --spider https://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
