FROM python:3.12-slim AS api
WORKDIR /app
RUN pip install --no-cache-dir \
    fastapi==0.115.0 'uvicorn[standard]==0.30.6' \
    pydantic==2.9.2 pydantic-settings==2.5.2 \
    asyncpg==0.29.0 sqlalchemy[asyncio]==2.0.35 pgvector==0.3.5 \
    psycopg[binary]==3.3.4 langgraph-checkpoint-postgres==3.1.0 \
    langchain-core==1.4.0 langgraph==1.2.2 \
    'PyJWT[crypto]==2.13.0' cryptography==46.0.7 \
    aiohttp==3.10.10 httpx==0.27.2 redis==5.1.1 \
    openai==2.38.0 numpy==2.3.5 scikit-learn==1.8.0 \
    jieba==0.42.1 opentelemetry-api==1.44.0 opentelemetry-sdk==1.44.0 \
    python-dotenv==1.0.1 nanoid==2.0.0 jinja2==3.1.6
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
