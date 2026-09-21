FROM python:3.11-slim-bookworm

ARG TARGETARCH=amd64
ARG OSSUTIL_VERSION=2.4.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fio ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Official ossutil 2.0 archives and SHA-256 values from Alibaba Cloud's install guide.
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) OSSUTIL_SHA256=85edf66b2fb7238f5c7e25cab820cf29312319fe4935b7c86a6b8485eb434f3c ;; \
      arm64) OSSUTIL_SHA256=7767240e9667d1f26fbe4e04f90b796b4356889e77d0076cc8a543c8ce081987 ;; \
      *) echo "Unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="ossutil-${OSSUTIL_VERSION}-linux-${TARGETARCH}.zip"; \
    curl --fail --location --show-error --silent --retry 3 \
      "https://gosspublic.alicdn.com/ossutil/v2/${OSSUTIL_VERSION}/${archive}" \
      --output "/tmp/${archive}"; \
    echo "${OSSUTIL_SHA256}  /tmp/${archive}" | sha256sum -c -; \
    unzip -p "/tmp/${archive}" "ossutil-${OSSUTIL_VERSION}-linux-${TARGETARCH}/ossutil" \
      > /usr/local/bin/ossutil; \
    chmod 0755 /usr/local/bin/ossutil; \
    rm "/tmp/${archive}"

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
