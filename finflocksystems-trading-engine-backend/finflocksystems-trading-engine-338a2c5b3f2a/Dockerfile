FROM flink:1.20.1-scala_2.12-java17 AS flink-client

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
COPY scripts/build_flink_python_runtime.sh /usr/local/bin/build-flink-python-runtime
RUN sed -i 's/\r$//' /usr/local/bin/build-flink-python-runtime \
    && chmod +x /usr/local/bin/build-flink-python-runtime \
    && /usr/local/bin/build-flink-python-runtime \
        /opt/flink-python-runtime/flink-python-runtime.zip

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
    FLINK_PYTHON_EXECUTABLE=pyenv/bin/python
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    openjdk-17-jre-headless \
    python3.11 \
    python3.11-venv \
    supervisor \
    && rm -rf /var/lib/apt/lists/*
COPY --from=flink-client /opt/flink /opt/flink
COPY --from=flink-python-runtime-builder \
    /opt/flink-python-runtime/flink-python-runtime.zip \
    /opt/flink-python-runtime/flink-python-runtime.zip
RUN curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
    -o /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar \
    https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.3.0-1.20/flink-sql-connector-kafka-3.3.0-1.20.jar
RUN /usr/bin/python3.11 -m venv /opt/pyflink-venv \
    && /opt/pyflink-venv/bin/pip install --no-cache-dir apache-flink==1.20.1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY .env.example .env
# Tolerate CRLF checkouts (Windows): a CR in a shebang line makes the script unrunnable.
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
# start-period covers migrations, Kafka/Flink bootstrap and the Redis wait before the first probe counts.
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1
ENTRYPOINT ["./entrypoint.sh"]
