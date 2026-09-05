FROM python:3.12-slim AS api
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.0 'uvicorn[standard]==0.30.6' pydantic==2.9.2 pydantic-settings==2.5.2 asyncpg==0.29.0 pgvector==0.3.5 'PyJWT[crypto]==2.13.0' cryptography==46.0.7 httpx==0.27.2 redis==5.1.1 python-dotenv==1.0.1 openai==2.38.0
COPY backend /app/backend
COPY scripts/experience.py scripts/experience_container.py /app/scripts/
CMD ["python", "scripts/experience_container.py"]

FROM node:22-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
ENV NODE_ENV=production EXPERIENCE_WEB_RUNTIME=1 NEXT_TELEMETRY_DISABLED=1
CMD ["sh", "-c", "node node_modules/next/dist/bin/next build && exec node node_modules/next/dist/bin/next start --hostname 0.0.0.0 --port 3106"]
