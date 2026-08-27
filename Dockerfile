FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    MYELIN_JAR_PATH=/app/jars \
    MYELIN_DB_PATH=/app/data/myelin.db \
    MYELIN_DOWNLOAD_CMS_ASSETS=false \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 appgroup \
    && useradd \
        --uid 10001 \
        --gid appgroup \
        --create-home \
        --shell /usr/sbin/nologin \
        appuser

WORKDIR /app

# Install the pinned local Myelin source and API dependencies.
COPY pyproject.toml README.md LICENSE ./
COPY myelin ./myelin
COPY requirements-bridge.txt ./

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && python -m pip install -r requirements-bridge.txt

# Copy the bridge application and the already-prepared CMS runtime assets.
COPY bridge ./bridge
COPY jars ./jars
COPY data/myelin.db ./data/myelin.db
COPY container/entrypoint.sh /entrypoint.sh

RUN chmod 0555 /entrypoint.sh \
    && chown -R appuser:appgroup /app/data \
    && chmod -R a+rX /app/jars /app/bridge /app/myelin \
    && chmod 0755 /app/data \
    && chmod 0644 /app/data/myelin.db

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
