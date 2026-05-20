FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# 构建期注入公开环境变量（NEXT_PUBLIC_* 会被 bake 进 JS bundle）
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_Y_WEBSOCKET_URL
ARG NEXT_PUBLIC_AMAP_JS_KEY
ARG NEXT_PUBLIC_AMAP_SECURITY_CODE
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_Y_WEBSOCKET_URL=$NEXT_PUBLIC_Y_WEBSOCKET_URL
ENV NEXT_PUBLIC_AMAP_JS_KEY=$NEXT_PUBLIC_AMAP_JS_KEY
ENV NEXT_PUBLIC_AMAP_SECURITY_CODE=$NEXT_PUBLIC_AMAP_SECURITY_CODE

RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next ./.next
# public/ 可以是空目录，RUN mkdir 防止目录不存在时 COPY 报错
RUN mkdir -p ./public
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

EXPOSE 3000
CMD ["npm", "start"]
