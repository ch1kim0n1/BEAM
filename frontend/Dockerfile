# Stage 1: build the Vite app.
FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci

COPY . .
# Point the frontend at the backend container (overridden at runtime via env).
# VITE_API_URL is a build-time var; bake in the /api-proxy default so the
# compose setup works without rebuilding.
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

# Stage 2: serve the built assets with nginx.
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html

# Rewrite all non-asset requests to index.html for the SPA router.
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  location / {\n    try_files $uri $uri/ /index.html;\n  }\n}\n' \
    > /etc/nginx/conf.d/default.conf

EXPOSE 80
