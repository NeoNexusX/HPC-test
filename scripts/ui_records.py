"""Run records read straight from the OSS archive, for the local UI.

A record is one attempt folder that holds manifest.json. run_instant_job.py uploads the manifest
last, so its presence marks a finished, archived attempt, whichever machine submitted the job:

    <prefix>/[<group or batch_id>/]<run_id>/<attempt>/{manifest.json, result.json, run.log, ...}
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import zipfile

MANIFEST = "manifest.json"
WORKERS = 16
LOG_LINES = 200


def _is_dataset(key: str) -> bool:
    return ".zarr/" in key and "/zarr-delta/" not in key


def _list_folder(bucket, folder: str) -> list:
    """Every object under folder, or nothing when it holds datasets (e.g. test_data/) rather than runs."""
    objects, token = [], ""
    while True:
        page = bucket.list_objects_v2(prefix=folder, continuation_token=token, max_keys=1000)
        if any(_is_dataset(obj.key) for obj in page.object_list):
            return []  # stop after one page: dataset folders hold many thousands of chunks
        objects += page.object_list
        if not page.is_truncated:
            return objects
        token = page.next_continuation_token


def _iso(timestamp) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def scan(bucket, prefix: str, manifests: dict) -> list[dict]:
    """All records under prefix/. manifests caches parsed manifest.json by key and ETag."""
    top = bucket.list_objects_v2(prefix=f"{prefix}/", delimiter="/", max_keys=1000)
    with ThreadPoolExecutor(WORKERS) as pool:
        objects = [obj for listing in pool.map(lambda folder: _list_folder(bucket, folder), top.prefix_list)
                   for obj in listing]
        folders = {obj.key[:-len(MANIFEST)]: obj for obj in objects if obj.key.endswith("/" + MANIFEST)}
        missing = [obj for obj in folders.values() if (obj.key, obj.etag) not in manifests]
        for obj, text in zip(missing, pool.map(lambda obj: bucket.get_object(obj.key).read(), missing)):
            try:
                manifests[obj.key, obj.etag] = json.loads(text)
            except ValueError:
                manifests[obj.key, obj.etag] = {}
    files = {folder: [] for folder in folders}
    for obj in objects:
        cut = obj.key.find("/")
        while cut != -1 and obj.key[:cut + 1] not in files:
            cut = obj.key.find("/", cut + 1)
        if cut != -1:
            files[obj.key[:cut + 1]].append({"name": obj.key[cut + 1:], "key": obj.key, "size": obj.size})
    return [_record(bucket.bucket_name, prefix, folder, obj, manifests[obj.key, obj.etag], files[folder])
            for folder, obj in folders.items()]


def _record(bucket_name: str, prefix: str, folder: str, manifest_obj, manifest: dict, files: list) -> dict:
    parts = folder[len(prefix) + 1:].rstrip("/").split("/")
    passed = manifest.get("overall_pass", manifest.get("tests_pass"))
    return {"id": f"{bucket_name}/{folder.rstrip('/')}", "bucket": bucket_name, "prefix": prefix,
            "group": "/".join(parts[:-2]), "run_id": manifest.get("run_id") or parts[max(0, len(parts) - 2)],
            "attempt": manifest.get("attempt_id") or parts[-1], "finished_at": _iso(manifest_obj.last_modified),
            "job_id": manifest.get("job_id"), "dataset": manifest.get("source"),
            "status": "succeeded" if passed else "failed",
            "umap_pass": manifest.get("umap_pass"), "artifacts_uploaded": manifest.get("artifacts_uploaded"),
            "oss_prefix": f"oss://{bucket_name}/{folder}", "file_count": len(files),
            "size": sum(file["size"] for file in files), "files": sorted(files, key=lambda file: file["name"])}


def summarize(result: dict) -> dict:
    """The fields worth comparing across runs; older benchmark records keep lscpu under checks."""
    cpu = result.get("cpu") or (result.get("checks") or {}).get("cpu") or {}
    lscpu = {row.get("field"): row.get("data") for row in ((cpu.get("lscpu") or {}).get("lscpu") or [])
             if isinstance(row, dict)}
    summary = cpu.get("summary") or {}
    stages = {name: value for name, value in (result.get("stages_seconds") or {}).items()
              if isinstance(value, (int, float))}
    raw_timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
    timings = {name: value for name, value in raw_timings.items()
               if isinstance(value, (int, float)) or value is None}
    pipeline_seconds = timings.get("pipeline_seconds")
    if isinstance(pipeline_seconds, (int, float)):
        total_seconds = round(pipeline_seconds, 1)
    elif stages and not all(name in stages for name in ("import", "download")):
        total_seconds = round(sum(stages.values()), 1)
    else:
        total_seconds = None
    return {"cpu_model": summary.get("model") or lscpu.get("Model name:"),
            "cpus": summary.get("cpus") or lscpu.get("CPU(s):"),
            "peak_rss_mib": result.get("peak_rss_mib"),
            "stages_seconds": stages, "timings": timings, "total_seconds": total_seconds,
            "labels": result.get("benchmark_labels") or (result.get("settings") or {}).get("benchmark_labels"),
            "umap": result.get("umap"), "dataset": result.get("dataset"), "disk": result.get("disk"),
            "image_git_sha": result.get("image_git_sha"), "hostname": result.get("hostname")}


def detail(bucket, record: dict) -> dict:
    by_name = {file["name"]: file["key"] for file in record["files"]}
    wanted = [name for name in ("result.json", "run.log") if name in by_name]
    with ThreadPoolExecutor(2) as pool:
        texts = dict(zip(wanted, pool.map(lambda name: bucket.get_object(by_name[name]).read(), wanted)))
    try:
        result = summarize(json.loads(texts["result.json"])) if "result.json" in texts else None
    except ValueError:
        result = None
    log = texts.get("run.log", b"").decode("utf-8", errors="replace").splitlines()[-LOG_LINES:]
    return {"result": result, "log": log}


def write_zip(entries: list[tuple], stream) -> None:
    """entries are (bucket, key, name in the zip); objects are fetched in parallel, written in order."""
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive, ThreadPoolExecutor(WORKERS) as pool:
        for (_, _, name), data in zip(entries, pool.map(lambda entry: entry[0].get_object(entry[1]).read(),
                                                         entries)):
            archive.writestr(name, data)
