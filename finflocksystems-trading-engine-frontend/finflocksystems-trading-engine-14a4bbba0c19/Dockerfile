FROM node:22-alpine AS build
WORKDIR /app
ARG VITE_APP_BASE_PATH=/qfsplatform/trading_engine_frontend/
ENV VITE_APP_BASE_PATH=$VITE_APP_BASE_PATH
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
ENV PORT=8000 \
    BACKEND_API_URL=https://finflockqfsplatform.cloud/qfsplatform/ai_trading_engine_backend1/api/v1
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
COPY runtime-config.template.js /etc/trading-engine/runtime-config.template.js
COPY docker-entrypoint.d/40-runtime-config.sh /docker-entrypoint.d/40-runtime-config.sh
RUN chmod +x /docker-entrypoint.d/40-runtime-config.sh
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 CMD wget -q --spider http://127.0.0.1:${PORT:-5173}/healthz || exit 1
CMD ["nginx", "-g", "daemon off;"]
