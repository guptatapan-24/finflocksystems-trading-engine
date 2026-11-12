# ============================================================================
# Stage 1: Build the React / TypeScript Frontend Application
# ============================================================================
FROM node:22-alpine AS frontend-builder
WORKDIR /app
ARG VITE_APP_BASE_PATH=/
ENV VITE_APP_BASE_PATH=$VITE_APP_BASE_PATH

COPY finflocksystems-trading-engine-frontend/finflocksystems-trading-engine-14a4bbba0c19/package.json \
     finflocksystems-trading-engine-frontend/finflocksystems-trading-engine-14a4bbba0c19/package-lock.json ./
RUN npm ci

COPY finflocksystems-trading-engine-frontend/finflocksystems-trading-engine-14a4bbba0c19/ ./
RUN npm run build

# ============================================================================
# Stage 2: Apache Flink Binaries
# ============================================================================
FROM flink:1.20.1-scala_2.12-java17 AS flink-client

# ============================================================================
# Stage 3: Build Flink Standalone Python Worker Runtime Archive
# ============================================================================
FROM python:3.12-slim-bookworm AS flink-python-runtime-builder
ARG PYTHON_RUNTIME_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
ARG PYTHON_RUNTIME_SHA256="03f15e19e2452641b6375b59ba094ff6cf2fc118315d24a6ca63ce60e4d4a6e0"
ENV PYTHON_RUNTIME_URL=${PYTHON_RUNTIME_URL} \
    PYTHON_RUNTIME_SHA256=${PYTHON_RUNTIME_SHA256} \
    SOURCE_DATE_EPOCH=1729036800

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    unzip \
    zip \
    && rm -rf /var/lib/apt/lists/*

COPY finflocksystems-trading-engine-backend/finflocksystems-trading-engine-338a2c5b3f2a/scripts/build_flink_python_runtime.sh /usr/local/bin/build-flink-python-runtime
RUN sed -i 's/\r$//' /usr/local/bin/build-flink-python-runtime \
    && chmod +x /usr/local/bin/build-flink-python-runtime \
    && /usr/local/bin/build-flink-python-runtime /opt/flink-python-runtime/flink-python-runtime.zip

# ============================================================================
# Stage 4: Complete Unified Trading Engine Container
# ============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLINK_HOME=/opt/flink \
    PATH=/opt/flink/bin:${PATH} \
    PYTHONPATH=/app:/app/streaming/flink \
    PYFLINK_CLIENT_EXECUTABLE=/opt/pyflink-venv/bin/python \
    FLINK_PYTHON_RUNTIME_MODE=archive \
    FLINK_PYTHON_ARCHIVE=/opt/flink-python-runtime/flink-python-runtime.zip \
    FLINK_PYTHON_ARCHIVE_TARGET=pyenv \
    FLINK_PYTHON_EXECUTABLE=pyenv/bin/python \
    PORT=8000

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nginx \
    openjdk-17-jre-headless \
    python3.11 \
    python3.11-venv \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Flink client binaries & runtime
COPY --from=flink-client /opt/flink /opt/flink
COPY --from=flink-python-runtime-builder \
    /opt/flink-python-runtime/flink-python-runtime.zip \
    /opt/flink-python-runtime/flink-python-runtime.zip

# Flink Kafka connector
RUN curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
    -o /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar \
    https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.3.0-1.20/flink-sql-connector-kafka-3.3.0-1.20.jar

# PyFlink virtualenv for job submissions
RUN /usr/bin/python3.11 -m venv /opt/pyflink-venv \
    && /opt/pyflink-venv/bin/pip install --no-cache-dir apache-flink==1.20.1

# Python dependencies
COPY finflocksystems-trading-engine-backend/finflocksystems-trading-engine-338a2c5b3f2a/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Backend application code
COPY finflocksystems-trading-engine-backend/finflocksystems-trading-engine-338a2c5b3f2a/ .
COPY finflocksystems-trading-engine-backend/finflocksystems-trading-engine-338a2c5b3f2a/.env.example .env

# Built Frontend assets into Nginx static root
COPY --from=frontend-builder /app/dist /usr/share/nginx/html

# Unified Nginx and Supervisor configs
COPY nginx.conf /etc/nginx/nginx.conf
COPY supervisord.conf /app/supervisord.conf

# Fix Windows CRLF line endings on shell scripts
RUN sed -i 's/\r$//' entrypoint.sh scripts/*.sh \
    && chmod +x entrypoint.sh scripts/ensure_flink_jobs.py scripts/build_flink_python_runtime.sh \
    && test -x /opt/pyflink-venv/bin/python \
    && /opt/pyflink-venv/bin/python -c "from pyflink.version import __version__; assert __version__ == '1.20.1'" \
    && test -x /opt/flink/bin/flink \
    && /opt/flink/bin/flink --version 2>&1 | grep -q 'Version: 1.20.1' \
    && java -version 2>&1 | grep -q 'version "17' \
    && test -f /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar \
    && test -r /opt/flink-python-runtime/flink-python-runtime.zip \
    && /opt/pyflink-venv/bin/python -c "from streaming.flink.jobs.python_worker import WorkerPythonConfig; WorkerPythonConfig.from_environment().validate()"

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]

