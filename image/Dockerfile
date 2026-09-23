FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY image/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY scripts/benchmark.py scripts/run_instant_job.py /app/

# Recorded in run.log so every result names the commit it was built from.
ARG GIT_SHA=unknown
ENV IMAGE_GIT_SHA=$GIT_SHA

USER appuser
# No ENTRYPOINT: the INSTANT job passes the full command, which then works whether
# Command is treated as entrypoint or as CMD.
CMD ["python", "/app/run_instant_job.py", "--help"]
