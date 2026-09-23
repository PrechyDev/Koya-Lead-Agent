# Koya Lead Research Agent — single always-on web service (Render).
# The Claude Agent SDK's pip wheel bundles the Claude Code CLI for Linux, so no Node install is needed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /srv
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY agent_plugin ./agent_plugin
COPY db ./db
COPY scripts ./scripts

USER app
EXPOSE 8000
# One worker: runs live in-process (asyncio tasks) and only one runs at a time.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
