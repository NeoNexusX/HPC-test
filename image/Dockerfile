FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    NUMBA_CACHE_DIR=/tmp/numba-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# massflow comes from PyPI; bump its pin in image/requirements.txt to ship algorithm changes.
# gcc only builds crcmod's C extension (oss2 CRC64 is ~20 MiB/s in pure Python) and is then removed.
COPY image/requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && python -m pip install -r /tmp/requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt \
    && python -c "import crcmod._crcfunext"

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY scripts/umap_job.py scripts/run_instant_job.py /app/

# Recorded in run.log so every result names the commit it was built from.
ARG GIT_SHA=unknown
ENV IMAGE_GIT_SHA=$GIT_SHA

USER appuser
# No ENTRYPOINT: the INSTANT job passes the full command, which then works whether
# Command is treated as entrypoint or as CMD.
CMD ["python", "/app/run_instant_job.py", "--help"]
