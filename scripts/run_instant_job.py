#!/usr/bin/env python3
"""Container entry point: test, archive logs, return failure on upload failure."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid

from benchmark import cpu_inventory, error_name, fio_benchmark, oss_benchmark, summarize, validate_settings


def make_bucket(settings: dict):
    import oss2
    parts = []
    index = 0
    while f"OSS_TOKEN_{index}" in os.environ:
        parts.append(os.environ[f"OSS_TOKEN_{index}"])
        index += 1
    if not parts:
        raise ValueError("missing runtime OSS STS token")
    expiration = dt.datetime.fromisoformat(os.environ["OSS_STS_EXPIRATION"].replace("Z", "+00:00"))
    if expiration <= dt.datetime.now(dt.timezone.utc):
        raise ValueError("runtime OSS STS token has expired")
    auth = oss2.StsAuth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"],
                        "".join(parts), auth_version="v4")
    # oss2's CRC64 falls back to pure Python (~20 MiB/s) without a compiler, which would cap the
    # measured throughput; the benchmark verifies SHA-256 outside the timed transfer instead.
    return oss2.Bucket(auth, settings["oss_endpoint"], settings["oss_bucket"],
                       region=settings["oss_region"], connect_timeout=60, enable_crc=False)


def run(settings: dict, bucket, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    attempt = uuid.uuid4().hex[:12]
    prefix = f"{settings['oss_prefix'].strip('/')}/{settings['run_id']}/{attempt}"
    log_path = output / "run.log"

    def log(message):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
        print(line, flush=True)
        with log_path.open("a") as stream:
            stream.write(line + "\n")

    result = {"run_id": settings["run_id"], "attempt_id": attempt,
              "image_git_sha": os.getenv("IMAGE_GIT_SHA"),
              "job_id": os.getenv("EHPC_JOB_ID"), "executor_id": os.getenv("EHPC_EXECUTOR_ID"),
              "hostname": socket.gethostname(), "settings": settings, "checks": {}}
    log(f"start run={settings['run_id']} attempt={attempt} host={result['hostname']} "
        f"image_git_sha={result['image_git_sha']}")
    for name, operation in (
        ("cpu", cpu_inventory),
        ("disk", lambda: fio_benchmark(settings, output)),
        ("oss", lambda: oss_benchmark(bucket, settings, prefix)),
    ):
        log(f"start {name}")
        try:
            result["checks"][name] = operation()
        except Exception as exc:
            result["checks"][name] = {"pass": False, "error": type(exc).__name__}
        log(f"finish {name} pass={result['checks'][name]['pass']}")
        try:
            lines = summarize(name, result["checks"][name])
        except Exception as exc:
            lines = [f"{name} summary unavailable: {type(exc).__name__}"]
        for line in lines:
            log(line)
    result["tests_pass"] = all(item["pass"] for item in result["checks"].values())
    (output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    log(f"tests finished tests_pass={result['tests_pass']}; archiving to oss://{settings['oss_bucket']}/{prefix}/")
    uploads = []
    upload_failed = False
    for path in sorted(output.iterdir()):
        if path.is_file():
            try:
                bucket.put_object_from_file(f"{prefix}/{path.name}", str(path))
                uploads.append({"file": path.name, "status": "passed"})
            except Exception as exc:
                upload_failed = True
                uploads.append({"file": path.name, "status": "failed", "error": error_name(exc)})
    upload_log = output / "oss-upload.json"
    upload_log.write_text(json.dumps(uploads, indent=2) + "\n")
    try:
        bucket.put_object_from_file(f"{prefix}/{upload_log.name}", str(upload_log))
    except Exception:
        upload_failed = True
    manifest = {"run_id": settings["run_id"], "attempt_id": attempt, "job_id": result["job_id"],
                "oss_prefix": f"oss://{settings['oss_bucket']}/{prefix}/",
                "tests_pass": result["tests_pass"], "artifacts_uploaded": not upload_failed,
                "overall_pass": result["tests_pass"] and not upload_failed}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        # Written last: a remote manifest with overall_pass=true is the completion marker.
        bucket.put_object_from_file(f"{prefix}/manifest.json", str(manifest_path))
    except Exception:
        upload_failed = True
        manifest.update(overall_pass=False, artifacts_uploaded=False, manifest_upload_failed=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    return 3 if upload_failed else (0 if result["tests_pass"] else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # INSTANT limits argument length, so the submitter may split the JSON into several arguments.
    parser.add_argument("--settings-json", required=True, nargs="+", help="non-secret benchmark settings")
    args = parser.parse_args()
    try:
        settings = json.loads("".join(args.settings_json))
        validate_settings(settings, runtime=True)
        bucket = make_bucket(settings)
        with tempfile.TemporaryDirectory(prefix="instant-results-") as directory:
            return run(settings, bucket, Path(directory))
    except Exception as exc:
        print(f"job setup failed: {error_name(exc)}; check settings and OSS STS credentials", flush=True)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
