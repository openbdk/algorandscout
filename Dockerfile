# Copyright (c) 2026 BANKON — all rights reserved. BANKON License (Apache-2.0).
FROM python:3.12-slim AS base

# Non-root by default. A read-only chain reader has no reason to run as root,
# and the container needs no write access to anything but /tmp.
RUN useradd --create-home --uid 10001 scout
WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY algorandscout ./algorandscout
RUN pip install --no-cache-dir '.[service]'

USER scout
EXPOSE 8100

ENV OPENBDK_HOST=0.0.0.0 \
    OPENBDK_PORT=8100 \
    ALGORAND_NETWORK=mainnet \
    PYTHONUNBUFFERED=1

# Readiness, not liveness: the container is unhealthy when it cannot answer
# questions, which includes the upstream being unreachable.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/readyz', timeout=8).status==200 else 1)"

CMD ["python", "-m", "algorandscout", "--host", "0.0.0.0", "--port", "8100"]
