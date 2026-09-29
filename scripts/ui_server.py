#!/usr/bin/env python3
"""Local-only API and built Vue frontend for E-HPC INSTANT operations."""
from __future__ import annotations

from collections import Counter
import copy
import datetime as dt
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
import oss2
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import run_batch
import submit_instant as submit
import ui_records


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
INSTANT_FILE = CONFIG_DIR / "instant.local.json"
BATCH_FILE = CONFIG_DIR / "batch.local.json"
ENV_FILE = ROOT / ".env"
WORK_DIR = ROOT / "work"
DIST_DIR = ROOT / "frontend" / "dist"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
RECENT_SECONDS = 3600  # finished UI launches stay visible this long; history comes from OSS
RECORDS_TTL_SECONDS = 60
TOKEN_REUSE_SECONDS = 3000  # ui_oss_token.py credentials last 3600 s

app = FastAPI(title="E-HPC INSTANT Control", version="1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


class ConfigUpdate(BaseModel):
    instant: Optional[dict] = None
    batch: Optional[dict] = None
    config_path: str = "config/instant.local.json"


class EnvChange(BaseModel):
    key: str
    value: Optional[str]


class EnvUpdate(BaseModel):
    changes: list[EnvChange]


class PreviewRequest(BaseModel):
    mode: Literal["instant", "batch"] = "instant"
    dataset: Optional[str] = None
    config_path: str = "config/instant.local.json"


class RunRequest(PreviewRequest):
    wait_timeout: int = Field(default=1800, ge=1, le=86400)
    poll_seconds: int = Field(default=10, ge=1, le=3600)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _profile(path_str: str) -> Path:
    path = Path(path_str)
    candidate = (path if path.is_absolute() else ROOT / path).resolve()
    name = candidate.name.lower()
    if (candidate.parent != CONFIG_DIR.resolve() or candidate.suffix != ".json"
            or name.startswith("batch.") or "policy" in name or name.endswith(".example.json")):
        raise HTTPException(400, "config_path must name an instant configuration in config/")
    if candidate.is_file():
        data = _read_json(candidate)
        if not isinstance(data.get("resources"), dict) or not isinstance(data.get("umap"), dict):
            raise HTTPException(400, "Selected JSON is not an instant configuration")
    elif not (name.startswith("instant.") or name.endswith(".local.json")):
        raise HTTPException(400, "New profiles must be instant.*.json or *.local.json")
    return candidate


def _read_json(path: Path, fallback: Path | None = None) -> dict:
    source = path if path.is_file() else fallback
    if source is None or not source.is_file():
        raise HTTPException(404, f"Missing {_relative(path)}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        raise HTTPException(422, f"Invalid JSON in {_relative(source)}") from None
    if not isinstance(data, dict):
        raise HTTPException(422, f"{_relative(source)} must contain a JSON object")
    return data


def _atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _validate_config(config: dict) -> None:
    try:
        submit.validate_config(config)
        submit.sdk_model(submit.build_request(config, "preview", registry_env={}))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from None


def _validate_batch(batch: dict) -> None:
    try:
        run_batch.validate_batch(batch)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(422, str(exc)) from None
    for index, run in enumerate(batch["runs"], 1):
        profile = _profile(run["config"])
        if not profile.is_file():
            raise HTTPException(422, f"Run {index} references a missing profile: {run['config']}")


@app.middleware("http")
async def no_store_api(request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _profile_configs() -> dict[str, dict]:
    """Every instant profile currently on disk in config/, keyed by its repo-relative path."""
    configs = {}
    for item in sorted(CONFIG_DIR.glob("*.json")):
        try:
            _profile(_relative(item))
        except HTTPException:
            continue
        configs[_relative(item)] = _read_json(item)
    return configs


@app.get("/api/config/files")
def get_config_files() -> dict:
    """Profiles and batch sequence as they are on disk now. The browser polls this, so files
    added or deleted outside the UI show up without reloading the page."""
    configs = _profile_configs()
    profiles = list(configs)
    if "config/instant.local.json" not in profiles:
        profiles.insert(0, "config/instant.local.json")
    batch = _read_json(BATCH_FILE) if BATCH_FILE.is_file() else {
        "batch_name": "umap-benchmark",
        "runs": [{"config": profiles[0], "repeat": 1}],
    }
    return {"profiles": profiles,
            "profile_resources": {path: config["resources"] for path, config in configs.items()},
            "batch": batch, "batch_saved": BATCH_FILE.is_file()}


@app.get("/api/config")
def get_config(path: str = "config/instant.local.json") -> dict:
    profile = _profile(path)
    fallback = CONFIG_DIR / "instant.example.json" if profile == INSTANT_FILE.resolve() else None
    return {"instant": _read_json(profile, fallback), "config_path": _relative(profile),
            "batch_path": _relative(BATCH_FILE), **get_config_files()}


@app.put("/api/config")
def put_config(payload: ConfigUpdate) -> dict:
    if payload.instant is None and payload.batch is None:
        raise HTTPException(422, "Provide instant or batch configuration")
    profile = _profile(payload.config_path)
    if payload.instant is not None:
        _validate_config(payload.instant)
    if payload.batch is not None:
        _validate_batch(payload.batch)
    if payload.instant is not None:
        _atomic_json(profile, payload.instant)
    if payload.batch is not None:
        # Preserve the editable batch order as provided by the browser.
        _atomic_json(BATCH_FILE, payload.batch)
    # A batch-only save must not report failure because the selected profile was deleted meanwhile.
    shown = profile if profile.is_file() else INSTANT_FILE
    return get_config(_relative(shown))


def _env_lines() -> list[str]:
    return ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.is_file() else []


def _env_values(lines: list[str]) -> dict[str, str]:
    values = {}
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = (part.strip() for part in raw.removeprefix("export ").split("=", 1))
        if ENV_NAME.fullmatch(key):
            values[key] = submit.parse_env_value(value)
    return values


def _env_keys(values: dict[str, str]) -> list[str]:
    examples = _env_values((ROOT / ".env.example").read_text(encoding="utf-8").splitlines())
    return list(dict.fromkeys([*examples, *values]))


def _effective_env() -> dict[str, str]:
    # A nonempty value saved in the UI wins. Empty placeholders behave like the CLI loader.
    return {**os.environ, **{key: value for key, value in _env_values(_env_lines()).items() if value}}


@app.get("/api/env")
def get_env() -> dict:
    values = _env_values(_env_lines())
    entries = []
    for key in _env_keys(values):
        file_value = values.get(key, "")
        process_value = os.environ.get(key, "")
        source = "file" if file_value else ("process" if process_value else "unset")
        value = file_value or process_value
        # The server only listens on 127.0.0.1, so values are shown as they are.
        entries.append({"key": key, "value": value, "present": bool(value), "source": source})
    return {"entries": entries}


@app.put("/api/env")
def put_env(payload: EnvUpdate) -> dict:
    changes = {}
    for change in payload.changes:
        if not ENV_NAME.fullmatch(change.key):
            raise HTTPException(422, f"Invalid environment variable name: {change.key}")
        if change.value is not None and any(char in change.value for char in "\n\r\0"):
            raise HTTPException(422, f"{change.key} must be one line without NUL")
        changes[change.key] = change.value
    lines = []
    written = set()
    for line in _env_lines():
        raw = line.strip().removeprefix("export ")
        key = raw.split("=", 1)[0].strip() if "=" in raw else None
        if key in changes:
            if key not in written and changes[key] is not None:
                lines.append(f"{key}={json.dumps(changes[key], ensure_ascii=False)}")
            written.add(key)
        else:
            lines.append(line)
    for key, value in changes.items():
        if key not in written and value is not None:
            lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    _atomic_text(ENV_FILE, "\n".join(lines).rstrip("\n") + "\n")
    return get_env()


@app.post("/api/preview")
def preview(payload: PreviewRequest) -> dict:
    if payload.mode == "instant":
        config = copy.deepcopy(_read_json(_profile(payload.config_path)))
        try:
            submit.select_dataset(config, payload.dataset)
            _validate_config(config)
            run_id = submit.new_run_id()
            request = submit.build_request(config, run_id, registry_env={})
            return {"mode": "instant", "valid": True,
                    "preview": {"run_id": run_id, "dataset": config["umap"]["source_zarr_path"],
                                "create_job_request": submit.redacted(request),
                                "oss_sts_session_policy": submit.oss_session_policy(config, run_id)}}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(422, str(exc)) from None
    batch = _read_json(BATCH_FILE)
    _validate_batch(batch)
    try:
        batch_id = submit.new_run_id()
        cells = run_batch.expand(batch, batch_id)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {"mode": "batch", "valid": True,
            "preview": {"batch_id": batch_id, "batch_name": batch.get("batch_name", "batch"),
                        "total": len(cells), "cells": [
                            {"run_id": cell["run_id"], "config_file": cell["config_file"],
                             "instance_type": cell["instance_type"],
                             "repeat_index": cell["repeat_index"],
                             "dataset": cell["config"]["umap"]["source_zarr_path"],
                             "image": cell["config"]["image"],
                             "resources": cell["config"]["resources"],
                             "oss_prefix": cell["config"]["umap"]["oss_prefix"]}
                            for cell in cells]}}


def _state_path(run_id: str) -> Path:
    return WORK_DIR / run_id / "ui_state.json"


def _load_state(run_id: str) -> dict:
    path = _state_path(run_id)
    if path.is_file():
        return _read_json(path)
    return {}


def _save_state(run_id: str, state: dict) -> None:
    _atomic_json(_state_path(run_id), state)


def _scan_log(log_path: Path, state: dict, offset: int) -> int:
    with log_path.open("r", encoding="utf-8", errors="replace") as stream:
        stream.seek(offset)
        while True:
            line = stream.readline()
            if not line:
                return stream.tell()
            line = line.rstrip("\r\n")
            if not line:
                continue
            state["latest_line"] = line[:512]
            status_match = re.search(r"job_id=(job-[A-Za-z0-9_-]+) state=([A-Za-z]+)", line)
            if status_match:
                state["job_id"] = status_match.group(1)
                state["cloud_status"] = status_match.group(2)
            progress_match = re.match(r"\[(\d+)/(\d+)\].* -> exit_code=", line)
            if progress_match:
                state["progress"] = int(progress_match.group(1))
                state["total"] = int(progress_match.group(2))


def _execute(run_id: str, payload: RunRequest) -> None:
    path = WORK_DIR / run_id
    state = _load_state(run_id)
    try:
        script = ROOT / "scripts" / ("submit_instant.py" if payload.mode == "instant" else "run_batch.py")
        command = [sys.executable, "-u", str(script), "--env-file", str(ENV_FILE),
                   "--wait-timeout", str(payload.wait_timeout), "--poll-seconds", str(payload.poll_seconds)]
        if payload.mode == "instant":
            command += ["--config", str(_profile(payload.config_path)), "--run-id", run_id, "--wait"]
            if payload.dataset:
                command += ["--dataset", payload.dataset]
        else:
            command += ["--batch", str(BATCH_FILE), "--batch-id", run_id]
        with (path / "ui.log").open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(command, cwd=ROOT, env=_effective_env(),
                                    stdout=log, stderr=subprocess.STDOUT)
            state["pid"] = proc.pid
            _save_state(run_id, state)
            offset = 0
            while proc.poll() is None:
                offset = _scan_log(path / "ui.log", state, offset)
                _save_state(run_id, state)
                time.sleep(0.5)
            _scan_log(path / "ui.log", state, offset)
            code = proc.wait()
        state["exit_code"] = code
        state["status"] = "succeeded" if code == 0 else ("timed_out" if code == 4 else "failed")
    except Exception as exc:
        state["status"] = "failed"
        state["latest_line"] = f"Local runner failed ({type(exc).__name__})"
    state["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_state(run_id, state)


@app.post("/api/runs", status_code=202)
def start_run(payload: RunRequest) -> dict:
    if payload.mode == "instant":
        config = copy.deepcopy(_read_json(_profile(payload.config_path)))
        try:
            submit.select_dataset(config, payload.dataset)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        _validate_config(config)
    else:
        if payload.dataset:
            raise HTTPException(422, "dataset override applies to instant runs only")
        batch = _read_json(BATCH_FILE)
        _validate_batch(batch)
        try:
            run_batch.expand(batch, "preview")
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
    run_id = submit.new_run_id()
    state = {"id": run_id, "mode": payload.mode, "status": "running",
             "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "config_path": payload.config_path if payload.mode == "instant" else "config/batch.local.json",
             "dataset": payload.dataset if payload.mode == "instant" else None,
             "progress": 0 if payload.mode == "batch" else None}
    _save_state(run_id, state)
    threading.Thread(target=_execute, args=(run_id, payload), daemon=True).start()
    return {"id": run_id, "mode": payload.mode, "status": "running",
            "created_at": state["created_at"]}


def _optional_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _run_record(path: Path) -> dict | None:
    run_id = path.name
    if not RUN_ID.fullmatch(run_id):
        return None
    state = _optional_json(path / "ui_state.json") or {}
    submission = _optional_json(path / "submission.json") or {}
    batch_summary = _optional_json(path / "batch_summary.json") or {}
    batch = batch_summary or _optional_json(path / "batch_progress.json") or {}
    if not state and not submission and not batch:
        return None
    mode = state.get("mode") or ("batch" if batch else "instant")
    status = state.get("status") or (
        ("succeeded" if batch["passed"] == batch["total"] else "failed")
        if "passed" in batch and "total" in batch else "recorded")
    if status == "running" and state.get("pid") and not _pid_alive(state["pid"]):
        # The local server may have restarted while the separate runner kept going.
        if batch_summary:
            status = "succeeded" if batch_summary.get("passed") == batch_summary.get("total") else "failed"
        else:
            lines = (path / "ui.log").read_text(encoding="utf-8", errors="replace").splitlines()[-20:] \
                if (path / "ui.log").is_file() else []
            states = [match.group(1) for line in lines
                      if (match := re.search(r" state=([A-Za-z]+)", line))]
            cloud_status = states[-1] if states else state.get("cloud_status")
            status = ("succeeded" if cloud_status in submit.SUCCESS else
                      "failed" if cloud_status in submit.TERMINAL else
                      "timed_out" if any("Local wait timed out" in line for line in lines) else "recorded")
    created_at = state.get("created_at") or dt.datetime.fromtimestamp(
        path.stat().st_mtime, dt.timezone.utc).isoformat()
    record = {"id": run_id, "mode": mode, "status": status, "created_at": created_at,
              "job_id": state.get("job_id") or submission.get("job_id"),
              "region": submission.get("region"),
              "config_path": state.get("config_path"),
              "dataset": submission.get("dataset") or state.get("dataset"),
              "image": submission.get("image"),
              "progress": state.get("progress", batch.get("completed")),
              "total": state.get("total", batch.get("total")),
              "latest_line": state.get("latest_line"),
              "cloud_status": state.get("cloud_status"),
              "exit_code": state.get("exit_code"),
              "finished_at": state.get("finished_at")}
    return record


def _apply_cloud_status(record: dict, status: str | None) -> None:
    """Use the GetJob state where the local runner cannot know how the job ended."""
    if status and status not in ("Unavailable", "Unknown"):
        record["cloud_status"] = status
        if record["status"] in ("recorded", "timed_out"):
            record["status"] = status


@app.get("/api/status")
def get_status() -> dict:
    """Runs started from this UI that are still going or ended within RECENT_SECONDS.

    Finished work is browsed from the OSS archive (/api/records), not from work/.
    """
    now = dt.datetime.now(dt.timezone.utc)
    runs = []
    if WORK_DIR.is_dir():
        for path in WORK_DIR.iterdir():
            if not (path / "ui_state.json").is_file() or (record := _run_record(path)) is None:
                continue
            try:
                ended = dt.datetime.fromisoformat(record["finished_at"]) if record["finished_at"] else None
            except (TypeError, ValueError):
                ended = None
            if record["status"] == "running" or (ended and (now - ended).total_seconds() < RECENT_SECONDS):
                runs.append(record)
    runs.sort(key=lambda item: item["created_at"], reverse=True)
    return {"runs": runs, "active_count": sum(r["status"] == "running" for r in runs)}


def _cloud_status(record: dict) -> dict | None:
    job_id = record.get("job_id")
    if not job_id:
        return None
    try:
        region = record.get("region")
        if not region:
            profile = _profile(record["config_path"]) if record.get("config_path") else INSTANT_FILE
            region = _read_json(profile, CONFIG_DIR / "instant.example.json")["region"]
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ui_get_job.py"),
                               region, job_id], cwd=ROOT, env=_effective_env(),
                              capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        return {"status": "Unavailable", "status_reason": "GetJob request failed"}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, HTTPException):
        return {"status": "Unavailable", "status_reason": "GetJob request failed"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, refresh: bool = True) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(400, "Invalid run id")
    path = WORK_DIR / run_id
    record = _run_record(path) if path.is_dir() else None
    if record is None:
        raise HTTPException(404, "Run not found")
    log_path = path / "ui.log"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:] if log_path.is_file() else []
    submission = _optional_json(path / "submission.json")
    batch_summary = _optional_json(path / "batch_summary.json")
    batch_progress = _optional_json(path / "batch_progress.json")
    children = list((batch_summary or batch_progress or {}).get("runs", []))
    current = (batch_progress or {}).get("current") if not batch_summary else None
    if isinstance(current, dict) and isinstance(current.get("run_id"), str) \
            and RUN_ID.fullmatch(current["run_id"]):
        current_summary = _optional_json(WORK_DIR / current["run_id"] / "submission.json") or {}
        current_job_id = current_summary.get("job_id")
        current_status = (record.get("cloud_status") if current_job_id == record.get("job_id")
                          else None) or "running"
        children.append({**current, "job_id": current_job_id,
                         "dataset": current_summary.get("dataset"),
                         "image": current_summary.get("image"),
                         "oss_prefix": current_summary.get("oss_prefix"),
                         "status": current_status})
    detail = {**record, "lines": lines,
              "summary": batch_summary or submission,
              "children": children}
    if refresh and record["mode"] == "instant":
        detail["cloud"] = _cloud_status(record)
        _apply_cloud_status(detail, (detail["cloud"] or {}).get("status"))
    return detail


@app.get("/api/batches")
def get_batches() -> dict:
    return {"batches": [run for run in get_status()["runs"] if run["mode"] == "batch"]}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    result = get_run(batch_id, refresh=False)
    if result["mode"] != "batch":
        raise HTTPException(404, "Batch not found")
    return result


def _archives() -> list[dict]:
    """Distinct OSS archives (bucket + prefix) that the instant profiles on disk write to."""
    archives = {}
    for config in _profile_configs().values():
        try:
            umap = config["umap"]
            key = (umap["oss_bucket"], umap["oss_prefix"].strip("/"))
            archives.setdefault(key, {
                "bucket": key[0], "prefix": key[1], "region": config["region"], "role_arn": config["oss_role_arn"],
                # Profiles name the in-VPC endpoint the job uses; this machine reads over the public one.
                "endpoint": umap["oss_endpoint"].replace("-internal.", ".")})
        except (KeyError, AttributeError, TypeError):
            continue
    return list(archives.values())


_tokens: dict = {}  # (bucket, prefix) -> (reuse deadline, read-only STS credentials)


def _oss_bucket(archive: dict):
    key = (archive["bucket"], archive["prefix"])
    deadline, credentials = _tokens.get(key, (0.0, None))
    if time.monotonic() > deadline:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ui_oss_token.py"), archive["region"],
                               archive["role_arn"], archive["bucket"], archive["prefix"]],
                              cwd=ROOT, env=_effective_env(), capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError("AssumeRole for the OSS archive failed")
        credentials = json.loads(proc.stdout)
        _tokens[key] = (time.monotonic() + TOKEN_REUSE_SECONDS, credentials)
    auth = oss2.StsAuth(credentials["AccessKeyId"], credentials["AccessKeySecret"], credentials["SecurityToken"])
    return oss2.Bucket(auth, archive["endpoint"], archive["bucket"], connect_timeout=10)


_records_lock = threading.Lock()
_records_cache: dict = {"at": None, "records": {}, "error": None, "synced_at": None}
_manifests: dict = {}  # (key, etag) -> parsed manifest.json; uploaded last, so never rewritten
OSS_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, oss2.exceptions.OssError)


def _records(force: bool = False) -> dict:
    """Every archived record, cached so the browser's 5-second polling does not list OSS each time."""
    fresh = _records_cache["at"] is not None and time.monotonic() - _records_cache["at"] < RECORDS_TTL_SECONDS
    if (fresh and not force) or not _records_lock.acquire(blocking=False):
        return _records_cache  # a concurrent request is already refreshing: serve the previous list
    try:
        records, failed = {}, []
        for archive in _archives():
            try:
                bucket = _oss_bucket(archive)
                records.update({record["id"]: {**record, "archive": archive}
                                for record in ui_records.scan(bucket, archive["prefix"], _manifests)})
            except OSS_ERRORS:
                failed.append(f"oss://{archive['bucket']}/{archive['prefix']}/")
        if failed:  # keep the previous list; the browser shows the error next to it
            _records_cache["error"] = f"Listing failed for {', '.join(failed)}"
        else:
            _records_cache.update(records=records, error=None, synced_at=dt.datetime.now(dt.timezone.utc).isoformat())
        _records_cache["at"] = time.monotonic()
    finally:
        _records_lock.release()
    return _records_cache


def _public(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in ("archive", "files")}


def _record(record_id: str) -> dict:
    record = _records()["records"].get(record_id)
    if record is None:
        raise HTTPException(404, "Record not found in the OSS archive")
    return record


@app.get("/api/records")
def get_records(refresh: bool = False) -> dict:
    cache = _records(refresh)
    records = sorted((_public(record) for record in cache["records"].values()),
                     key=lambda record: record["finished_at"], reverse=True)
    groups = Counter(record["group"] for record in records)
    return {"records": records, "groups": [{"name": name, "count": count} for name, count in sorted(groups.items())],
            "archives": [f"oss://{archive['bucket']}/{archive['prefix']}/" for archive in _archives()],
            "error": cache["error"], "synced_at": cache["synced_at"]}


@app.get("/api/records/detail")
def get_record(id: str) -> dict:
    record = _record(id)
    try:
        detail = ui_records.detail(_oss_bucket(record["archive"]), record)
    except OSS_ERRORS:
        raise HTTPException(502, "Reading the record from OSS failed") from None
    files = [{"name": file["name"], "size": file["size"]} for file in record["files"]]
    return {**_public(record), "files": files, **detail}


def _download_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "records"


@app.get("/api/records/file")
def get_record_file(id: str, name: str, download: bool = False) -> StreamingResponse:
    record = _record(id)
    file = next((file for file in record["files"] if file["name"] == name), None)
    if file is None:
        raise HTTPException(404, "File not found in this record")
    try:
        body = _oss_bucket(record["archive"]).get_object(file["key"])
    except OSS_ERRORS:
        raise HTTPException(502, "Reading the file from OSS failed") from None
    media = "text/plain; charset=utf-8" if name.endswith(".log") else (
        mimetypes.guess_type(name)[0] or "application/octet-stream")
    disposition = "attachment" if download else "inline"
    return StreamingResponse(iter(lambda: body.read(1 << 16), b""), media_type=media, headers={
        "Content-Disposition": f'{disposition}; filename="{_download_name(Path(name).name)}"'})


@app.get("/api/records/download")
def download_records(id: Optional[str] = None, group: Optional[str] = None) -> StreamingResponse:
    """A zip of one record (id), one group folder (group; empty for ungrouped runs) or everything."""
    records = list(_records()["records"].values())
    if id is not None:
        selected = [_record(id)]
        name = f"{selected[0]['run_id']}-{selected[0]['attempt']}"
    else:
        selected = [record for record in records if group is None or record["group"] == group]
        name = "oss-records" if group is None else (group or "single-runs")
    if not selected:
        raise HTTPException(404, "No records to download")
    try:
        buckets = {}
        for record in selected:
            archive = record["archive"]
            if (archive["bucket"], archive["prefix"]) not in buckets:
                buckets[archive["bucket"], archive["prefix"]] = _oss_bucket(archive)
        # Names inside the zip keep the OSS layout below the archive prefix.
        entries = [(buckets[record["bucket"], record["prefix"]], file["key"], file["key"][len(record["prefix"]) + 1:])
                   for record in selected for file in record["files"]]
        stream = tempfile.SpooledTemporaryFile(max_size=64 << 20)
        ui_records.write_zip(entries, stream)
    except OSS_ERRORS:
        raise HTTPException(502, "Downloading records from OSS failed") from None

    def chunks():
        try:
            stream.seek(0)
            while chunk := stream.read(1 << 20):
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{_download_name(name)}.zip"'})


@app.get("/{asset_path:path}", include_in_schema=False)
def serve_frontend(asset_path: str) -> Response:
    if asset_path.startswith("api/"):
        raise HTTPException(404, "API route not found")
    if not DIST_DIR.is_dir():
        raise HTTPException(404, "Vue frontend has not been built")
    target = (DIST_DIR / asset_path).resolve()
    if target.is_file() and target.is_relative_to(DIST_DIR.resolve()):
        return FileResponse(target)
    return FileResponse(DIST_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
