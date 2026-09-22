#!/usr/bin/env bash
set -Eeuo pipefail

# Slurm compute-node entrypoint. Values containing spaces are base64 encoded by
# trigger_epc_test.py so sbatch does not reinterpret them as export syntax.

decode() {
  printf '%s' "$1" | base64 --decode
}

required_env=(TEST_IMAGE_B64 RUN_ID_B64 OSS_URI_B64 RESULT_ROOT_B64 SHARED_DIR_B64 SSD_DIR_B64)
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "missing required environment variable: $name" >&2
    exit 64
  fi
done

TEST_IMAGE="$(decode "$TEST_IMAGE_B64")"
RUN_ID="$(decode "$RUN_ID_B64")"
OSS_URI="$(decode "$OSS_URI_B64")"
RESULT_ROOT="$(decode "$RESULT_ROOT_B64")"
SHARED_DIR="$(decode "$SHARED_DIR_B64")"
SSD_DIR="$(decode "$SSD_DIR_B64")"
EPC_INSTA_CMD="$(decode "${EPC_INSTA_CMD_B64:-}")"
SSD_SIZE="$(decode "${SSD_SIZE_B64:-MUc=}")"

RUN_DIR="${RESULT_ROOT%/}/${RUN_ID}"
OSS_TARGET="${OSS_URI%/}/${RUN_ID}"
CONTAINER_SHARED_DIR="/shared"
mkdir -p "$RUN_DIR"
chmod u+rwx "$RUN_DIR"

export TEST_IMAGE RUN_ID OSS_URI RESULT_ROOT SHARED_DIR SSD_DIR EPC_INSTA_CMD SSD_SIZE
export SLURM_JOB_ID="${SLURM_JOB_ID:-}"
export HOSTNAME="$(hostname)"

pull_exit=0
test_exit=125
upload_exit=125

# Private registry credentials should come from a node credential helper or a
# protected scheduler environment. They are never CLI arguments. Public GHCR
# images do not need this login step.
if [[ -n "${REGISTRY:-}" && -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
  printf '%s' "$REGISTRY_PASSWORD" | docker login "$REGISTRY" \
    --username "$REGISTRY_USERNAME" --password-stdin >"$RUN_DIR/registry-login.log" 2>&1 || true
fi

docker pull "$TEST_IMAGE" >"$RUN_DIR/docker-pull.log" 2>&1 || pull_exit=$?

if [[ "$pull_exit" -eq 0 ]]; then
  docker_args=(
    run --rm
    --user "$(id -u):$(id -g)"
    --mount "type=bind,src=$RUN_DIR,dst=/work"
  )
  container_args=(--out /work/smoke-result.json)
  if [[ -d "$SHARED_DIR" ]]; then
    docker_args+=(
      --mount "type=bind,src=$SHARED_DIR,dst=$CONTAINER_SHARED_DIR"
      --env "EPC_SHARED_DIR=$CONTAINER_SHARED_DIR"
    )
  fi
  if [[ "${REQUIRE_SHARED:-0}" == "1" ]]; then
    container_args+=(--require-shared)
  fi
  if [[ -n "$EPC_INSTA_CMD" ]]; then
    docker_args+=(--env "EPC_INSTA_CMD=$EPC_INSTA_CMD")
  fi
  if [[ "${SSD_TEST:-0}" == "1" ]]; then
    docker_args+=(--mount "type=bind,src=$SSD_DIR,dst=$SSD_DIR")
    container_args+=(--ssd-test --ssd-dir "$SSD_DIR" --ssd-size "$SSD_SIZE" --ssd-runtime "${SSD_RUNTIME:-30}")
  fi
  docker_args+=("$TEST_IMAGE" "${container_args[@]}")
  set +e
  docker "${docker_args[@]}" >"$RUN_DIR/docker.log" 2>&1
  test_exit=$?
  set -e
fi

write_manifest() {
  local upload_status="$1"
  local upload_code="$2"
  MANIFEST_PATH="$RUN_DIR/manifest.json" \
  TEST_EXIT="$test_exit" PULL_EXIT="$pull_exit" UPLOAD_STATUS="$upload_status" \
  UPLOAD_EXIT="$upload_code" OSS_TARGET="$OSS_TARGET" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

pull_exit = int(os.environ["PULL_EXIT"])
test_exit = int(os.environ["TEST_EXIT"])
upload_exit = int(os.environ["UPLOAD_EXIT"])
manifest = {
    "schema_version": 1,
    "run_id": os.environ["RUN_ID"],
    "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
    "hostname": os.environ.get("HOSTNAME"),
    "image": os.environ["TEST_IMAGE"],
    "test_exit_code": test_exit,
    "image_pull_exit_code": pull_exit,
    "overall_pass": pull_exit == 0 and test_exit == 0,
    "upload_status": os.environ["UPLOAD_STATUS"],
    "upload_exit_code": upload_exit,
    "oss_prefix": os.environ["OSS_TARGET"],
}
Path(os.environ["MANIFEST_PATH"]).write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

write_manifest pending 125
upload_log="$(mktemp)"
if command -v "${OSSUTIL_BIN:-ossutil}" >/dev/null 2>&1; then
  ossutil_bin="${OSSUTIL_BIN:-ossutil}"
  set +e
  "$ossutil_bin" cp -r "$RUN_DIR/" "$OSS_TARGET/" >"$upload_log" 2>&1
  upload_exit=$?
  set -e
else
  echo "ossutil is not installed on the compute node" >"$upload_log"
  upload_exit=127
fi

upload_status="passed"
if [[ "$upload_exit" -ne 0 ]]; then
  upload_status="failed"
fi
write_manifest "$upload_status" "$upload_exit"

# Re-upload the final manifest so OSS contains the definitive upload status.
if [[ "$upload_exit" -eq 0 ]]; then
  set +e
  "${OSSUTIL_BIN:-ossutil}" cp "$RUN_DIR/manifest.json" "$OSS_TARGET/manifest.json" >>"$upload_log" 2>&1
  final_manifest_exit=$?
  set -e
  if [[ "$final_manifest_exit" -ne 0 ]]; then
    upload_exit="$final_manifest_exit"
    write_manifest failed "$upload_exit"
  fi
fi
mv "$upload_log" "$RUN_DIR/oss-upload.log"
if [[ "$upload_exit" -eq 0 ]]; then
  set +e
  "${OSSUTIL_BIN:-ossutil}" cp "$RUN_DIR/oss-upload.log" "$OSS_TARGET/oss-upload.log" >/dev/null 2>&1
  log_upload_exit=$?
  set -e
  if [[ "$log_upload_exit" -ne 0 ]]; then
    upload_exit="$log_upload_exit"
    write_manifest failed "$upload_exit"
    "${OSSUTIL_BIN:-ossutil}" cp "$RUN_DIR/manifest.json" "$OSS_TARGET/manifest.json" >/dev/null 2>&1 || true
  fi
fi

echo "run_id=$RUN_ID"
echo "result_dir=$RUN_DIR"
echo "oss_uri=$OSS_TARGET/"
echo "image_pull_exit=$pull_exit"
echo "test_exit=$test_exit"
echo "upload_exit=$upload_exit"

if [[ "$pull_exit" -ne 0 || "$test_exit" -ne 0 ]]; then
  exit 2
fi
if [[ "$upload_exit" -ne 0 ]]; then
  exit 3
fi
