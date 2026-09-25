FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

# numba keys its disk cache on the CPU name and features. A fixed x86-64-v3 (AVX2) target makes
# the cache built on the CI runner valid on INSTANT hosts, instead of being recompiled every job.
ENV NUMBA_CACHE_DIR=/home/appuser/.cache/numba \
    NUMBA_CPU_NAME=x86-64-v3 \
    NUMBA_CPU_FEATURES=""

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

USER appuser
# Run UMAP once so the numba cache and matplotlib font cache ship in the image. Functions that
# umap-learn does not mark cacheable still compile in each job. MassFlow writes logs/ into the
# working directory, hence /tmp.
RUN cd /tmp && PYTHONPATH=/app python -c "import umap_job; umap_job.warm_up()" \
    && rm -rf /tmp/logs \
    && test -n "$(find "$NUMBA_CACHE_DIR" -name '*.nbi' -print -quit)"

# Recorded in run.log so every result names the commit it was built from.
ARG GIT_SHA=unknown
ENV IMAGE_GIT_SHA=$GIT_SHA

# No ENTRYPOINT: the INSTANT job passes the full command, which then works whether
# Command is treated as entrypoint or as CMD.
CMD ["python", "/app/run_instant_job.py", "--help"]
