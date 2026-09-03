# jira-workbox — hosted (multi-user) container image.
#
# Each user logs in with their own Atlassian API token; the token lives ONLY in
# this process's memory (never on disk). App state that IS written — the rollback
# journal and pinned license groups — goes to /data, mount a volume there to keep
# it across restarts (see k8s/jira-workbox.yaml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WORKBOX_HOSTED=1 \
    WORKBOX_LOG_DIR=/data

WORKDIR /app

# loose deps (not the uv lock) so the image builds on any 3.11+ base. keyring is
# installed but unused in hosted mode — its access is guarded (core.auth._kr_get).
RUN pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" \
                "keyring>=25.0" "pydantic>=2.7"

COPY app.py ./
COPY core ./core
COPY tasks ./tasks
COPY static ./static

# non-root, with a writable state dir
RUN useradd -u 10001 -m appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000
# bind 0.0.0.0 so the Service can reach it (localhost would be pod-only)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
