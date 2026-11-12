FROM python:3.12-slim

ARG TARGETARCH
ARG IBC_VERSION=3.23.0
# IBKR serves only a moving `stable` URL here, so the major this resolves to
# changes under us: two builds of one commit picked up different majors, and
# the newer one bundled its JRE somewhere the previous layout did not, leaving
# the install4j launcher pointing into the installer's deleted temp directory.
# The install step below normalizes whatever layout the build produces, and the
# verify + `java -version` that follow fail the build rather than publish an
# image whose launcher cannot find a JRE. Nothing here may name a major: that
# is detected at build and launch time, and a test asserts its absence.
ARG IB_GATEWAY_INSTALLER_URL=https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
ARG OCI_SOURCE=https://github.com/rachitm0897/trading-engine
ARG OCI_VERSION=unspecified
ARG OCI_REVISION=unspecified
ARG OCI_CREATED=unspecified
ARG OCI_TITLE="Trading Engine IB Gateway"
ARG OCI_DESCRIPTION="One-session IBKR Gateway, IBC, noVNC, and Django API service"

LABEL org.opencontainers.image.source="$OCI_SOURCE" \
      org.opencontainers.image.version="$OCI_VERSION" \
      org.opencontainers.image.revision="$OCI_REVISION" \
      org.opencontainers.image.created="$OCI_CREATED" \
      org.opencontainers.image.title="$OCI_TITLE" \
      org.opencontainers.image.description="$OCI_DESCRIPTION"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN selected_arch="${TARGETARCH:-amd64}" && \
    if [ "$selected_arch" != "amd64" ]; then \
      echo "Unsupported target architecture: IB Gateway is packaged only for linux/amd64" >&2; \
      exit 64; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip nginx supervisor xvfb fluxbox x11vnc x11-utils novnc websockify default-jre tini gettext-base procps \
    libxrender1 libxtst6 libxi6 libfreetype6 libgtk-3-0 libasound2 libnss3 libxss1 && rm -rf /var/lib/apt/lists/*
COPY ibgateway_version.py /usr/local/bin/ibgateway-version
COPY third_party/IBCLinux-3.23.0.zip /tmp/ibc.zip
RUN useradd -m -u 1000 ibgateway && mkdir -p /opt/ibc /opt/ibgateway /data /run/nginx && \
    chmod +x /usr/local/bin/ibgateway-version && \
    echo "0bd03c713f04c0a64367abbd3d5a40d228fcc2fb969c927bd7cdbd85baddac4b  /tmp/ibc.zip" | sha256sum -c - && \
    unzip -q /tmp/ibc.zip -d /opt/ibc && chmod +x /opt/ibc/*.sh && rm /tmp/ibc.zip && \
    curl -fsSL "$IB_GATEWAY_INSTALLER_URL" -o /tmp/ibgateway.sh && chmod +x /tmp/ibgateway.sh && \
    /tmp/ibgateway.sh -q -dir /opt/ibgateway && rm /tmp/ibgateway.sh && \
    /usr/local/bin/ibgateway-version install && \
    /usr/local/bin/ibgateway-version verify && \
    "$(cat /opt/ibgateway/.install4j/inst_jre.cfg)/bin/java" -version && \
    chown -R ibgateway:ibgateway /home/ibgateway /opt/ibc /opt/ibgateway /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY manage.py runtime_config.py ensure_database.py entrypoint.sh start-ibgateway.sh wait-for-x.sh supervisord.conf nginx.conf.template proxy_headers.conf ./
COPY broker ./broker
COPY config ./config
COPY gateway_service ./gateway_service
RUN chmod +x entrypoint.sh start-ibgateway.sh wait-for-x.sh /opt/ibc/scripts/*.sh && mkdir -p /var/log/supervisor
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fsS http://127.0.0.1:${PORT:-8080}/healthz || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "./entrypoint.sh"]
