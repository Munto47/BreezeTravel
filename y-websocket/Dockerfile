FROM node:20.18.3-alpine3.21
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY server.js ./

ENV HOST=0.0.0.0
ENV PORT=1234
EXPOSE 1234
CMD ["npm", "start"]
