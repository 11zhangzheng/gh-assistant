FROM python:3.12-slim

RUN pip install --no-cache-dir pytest==8.3.5 pytest-cov==6.0.0 \
    && useradd --uid 65532 --create-home runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace

WORKDIR /workspace
USER 65532:65532

ENTRYPOINT []
