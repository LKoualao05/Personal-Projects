# Cloud Run image: ADK agent API + dev UI, tracker MCP server, scheduled sync.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    TOKEN_PATH=/secrets/token.json \
    GOOGLE_GENAI_USE_VERTEXAI=TRUE

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config.json main.py ./
COPY tracker/ tracker/
COPY agents/ agents/

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

# One worker: the sync endpoint holds an in-process lock and the service runs
# with max-instances=1, so there is exactly one writer to the sheet.
CMD ["sh", "-c", "exec uvicorn tracker.server:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
