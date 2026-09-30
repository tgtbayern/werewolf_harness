# Build context: the werewolf_harness folder (see deploy/docker-compose.yml)
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app
WORKDIR /app
RUN pip install uv==0.8.* && useradd -m -u 10001 app
COPY pyproject.toml /app/werewolf_harness/pyproject.toml
RUN uv pip install --system -r /app/werewolf_harness/pyproject.toml opentelemetry-exporter-otlp-proto-http
COPY --chown=app:app . /app/werewolf_harness
USER app
EXPOSE 8402 9100
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8402/healthz').status==200 else 1)" || exit 1
# --timeout-graceful-shutdown: SIGTERM -> lifespan drains (releases leases) before the process exits
CMD ["uvicorn", "werewolf_harness.app.api.main:app", "--host", "0.0.0.0", "--port", "8402", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
