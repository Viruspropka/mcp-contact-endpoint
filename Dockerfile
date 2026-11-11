FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
# Runtime pins from requirements.txt (without pytest and the editable line), then the package.
RUN grep -v -e '^-e' -e '^pytest' -e '^#' requirements.txt > /tmp/runtime.txt \
 && pip install --no-cache-dir -r /tmp/runtime.txt . \
 && useradd --system --uid 10001 app \
 && mkdir -p /data && chown app /data

USER app
ENV CONTACT_HOST=0.0.0.0 \
    CONTACT_PORT=8000 \
    CONTACT_FILE_SINK_PATH=/data/outbox.jsonl \
    CONTACT_AUDIT_DB_PATH=/data/audit.sqlite
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["contact-endpoint", "serve"]
