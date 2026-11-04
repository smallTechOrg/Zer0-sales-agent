# Image for the Zer0 sales agent on Cloud Run.
# Built and deployed by .github/workflows/deploy.yml.
FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY code/requirements.txt .
RUN pip install -r requirements.txt

# The app reads prompts/, templates/ and static/ relative to this directory.
COPY code/ .

# Run as an unprivileged user.
RUN useradd --system --uid 10001 app && chown -R app /app
USER app

# Cloud Run sets PORT (8080 by default).
ENV PORT=8080
EXPOSE 8080

# One worker process: the DB pool is deliberately tiny (DB_POOL_MAX_SIZE=2)
# and the in-memory state (scheduler, locks) is per process. Threads give the
# concurrency; requests spend their time waiting on the LLM, not on CPU.
# --timeout 0: Cloud Run enforces the request timeout itself.
CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 8 --timeout 0 \
    --access-logfile - --error-logfile - app:app
