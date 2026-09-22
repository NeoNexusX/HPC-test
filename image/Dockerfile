FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY image/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app /work /shared \
    && chown -R appuser:appuser /app /work /shared

WORKDIR /app
COPY scripts/epc_insta_smoke.py /app/epc_insta_smoke.py
RUN chmod 0555 /app/epc_insta_smoke.py

USER appuser
ENTRYPOINT ["python", "/app/epc_insta_smoke.py"]
CMD ["--out", "/work/smoke-result.json"]


