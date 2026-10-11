# Build and install the wheel with only the demo's locked runtime dependencies.
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir poetry==1.8.3 \
    && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml poetry.lock README.md LICENSE ./
RUN poetry install --only main,examples --extras all --no-interaction --no-ansi --no-root
COPY moderato ./moderato
RUN poetry build --format wheel \
    && pip install --no-cache-dir --no-deps dist/*.whl

# Tests have their own target: developer tools never enter the runtime image.
FROM builder AS test
RUN poetry install --only main,examples,dev --extras all --no-interaction --no-ansi --no-root
COPY examples ./examples
COPY tests ./tests
CMD ["pytest", "tests/", "-v", "--tb=short"]

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
RUN useradd -m -u 1000 moderato
COPY --chown=moderato:moderato examples ./examples
USER moderato
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import json,os,urllib.request; response=urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/status',timeout=2); raise SystemExit(json.load(response)['redis_connected'] is not True)"
CMD ["sh", "-c", "exec uvicorn examples.fastapi_app:app --host 0.0.0.0 --port \"$PORT\""]

# Run the real performance harness, not the algorithm demonstration.
FROM runtime AS benchmark
COPY --chown=moderato:moderato benchmarks ./benchmarks
CMD ["python", "benchmarks/performance.py", "--quick"]

# Keep the demo runtime as the default docker build target.
FROM runtime AS final
