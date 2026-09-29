#!/usr/bin/env python3
"""Container entry point: run MassFlow UMAP on an OSS Zarr dataset, upload results and logs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from importlib import metadata
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import sys
import tempfile
import time
import traceback
import uuid

from umap_job import (check_disk_space, cpu_inventory, cpu_lines, diff_snapshot, download_objects, error_detail,
                      error_name, import_massflow, list_source_objects, run_umap, snapshot_dir,
                      split_download_objects, upload_files, validate_settings, DEFAULT_DOWNLOAD_WORKERS,
                      LIST_WORKERS)


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
    # CRC64 stays on: this moves real data. The image compiles crcmod's C extension for speed.
    workers = settings.get("download_workers", DEFAULT_DOWNLOAD_WORKERS)
    session = oss2.http.Session(pool_size=max(workers, LIST_WORKERS))
    return oss2.Bucket(auth, settings["oss_endpoint"], settings["oss_bucket"],
                       region=settings["oss_region"], connect_timeout=60, session=session)


def package_versions() -> dict:
    """Numerical stack actually installed, since only massflow itself is pinned."""
    versions = {}
    for name in ("massflow", "umap-learn", "pynndescent", "numba", "numpy", "scikit-learn", "zarr"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def peak_rss_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 2**20 if sys.platform == "darwin" else rss / 1024  # bytes on macOS, KiB on Linux


def _rate(mib: float, seconds) -> float | None:
    """MiB/s, or None when the stage is too fast to measure (avoids dividing by zero)."""
    return round(mib / seconds, 2) if isinstance(seconds, (int, float)) and seconds > 0 else None


def throughput(stages_seconds: dict, download_bytes: int, uploaded_bytes: int, umap: dict) -> dict:
    """Time and speed of each data-moving stage, recorded on the node that ran the job."""
    download_mib, upload_mib = download_bytes / 2**20, uploaded_bytes / 2**20
    dl, um, ul = stages_seconds.get("download"), stages_seconds.get("umap"), stages_seconds.get("upload")
    return {
        "download": {"seconds": dl, "mib": round(download_mib, 2), "mib_per_s": _rate(download_mib, dl)},
        "compute": {"seconds": um, "matrix_mib": umap["matrix_mib"], "pixels": umap["pixels"],
                    "mib_per_s": _rate(umap["matrix_mib"], um),
                    "pixels_per_s": round(umap["pixels"] / um, 1) if isinstance(um, (int, float)) and um > 0 else None},
        "upload": {"seconds": ul, "mib": round(upload_mib, 2), "mib_per_s": _rate(upload_mib, ul)},
    }


def throughput_lines(metrics: dict) -> list[str]:
    return [f"throughput download={metrics['download']['mib']}MiB in {metrics['download']['seconds']}s "
            f"({metrics['download']['mib_per_s']}MiB/s)",
            f"throughput compute={metrics['compute']['matrix_mib']}MiB in {metrics['compute']['seconds']}s "
            f"({metrics['compute']['mib_per_s']}MiB/s, {metrics['compute']['pixels_per_s']}px/s)",
            f"throughput upload={metrics['upload']['mib']}MiB in {metrics['upload']['seconds']}s "
            f"({metrics['upload']['mib_per_s']}MiB/s)"]


def timing_summary(stages: dict, intervals: dict, pipeline_start: float,
                   pipeline_end: float, data_ready: float | None) -> dict:
    """Wall-clock timings; import and download are measured independently while they overlap."""
    overlap = None
    if "import" in intervals and "download" in intervals:
        import_start, import_end = intervals["import"]
        download_start, download_end = intervals["download"]
        overlap = round(max(0.0, min(import_end, download_end) - max(import_start, download_start)), 3)
    return {"list_seconds": stages.get("list"),
            "massflow_import_seconds": stages.get("import"),
            "download_seconds": stages.get("download"),
            "import_download_overlap_seconds": overlap,
            "list_to_umap_seconds": round(data_ready - pipeline_start, 3) if data_ready is not None else None,
            "pipeline_seconds": round(pipeline_end - pipeline_start, 3)}


def run(settings: dict, bucket, output: Path, work: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    attempt = uuid.uuid4().hex[:12]
    prefix = f"{settings['oss_prefix'].strip('/')}/{settings['run_id']}/{attempt}"
    source = settings["source_zarr_path"]
    # Test runs keep the dataset read-only; write_back stores analysis/umap in the source like FC did.
    results_prefix = source if settings["write_back"] else f"{prefix}/zarr-delta"
    log_path = output / "run.log"
    stage_intervals = {}

    def log(message):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
        print(line, flush=True)
        with log_path.open("a") as stream:
            stream.write(line + "\n")

    def stage(name, operation):
        log(f"start {name}")
        start = time.perf_counter()
        succeeded = False
        try:
            value = operation()
            succeeded = True
            return value
        finally:
            end = time.perf_counter()
            stage_intervals[name] = (start, end)
            result["stages_seconds"][name] = round(end - start, 3)
            log(f"{'finish' if succeeded else 'failed'} {name} {result['stages_seconds'][name]:.3f}s")

    bucket_url = f"oss://{settings['oss_bucket']}"
    result = {"run_id": settings["run_id"], "attempt_id": attempt,
              "image_git_sha": os.getenv("IMAGE_GIT_SHA"), "packages": package_versions(),
              "job_id": os.getenv("EHPC_JOB_ID"), "executor_id": os.getenv("EHPC_EXECUTOR_ID"),
              "hostname": socket.gethostname(), "settings": settings,
              "benchmark_labels": settings.get("benchmark_labels"),
              "source": f"{bucket_url}/{source}/", "results": f"{bucket_url}/{results_prefix}/",
              "stages_seconds": {}, "umap_pass": False}
    log(f"start run={settings['run_id']} attempt={attempt} host={result['hostname']} "
        f"image_git_sha={result['image_git_sha']}")
    log("packages " + " ".join(f"{name}={version}" for name, version in result["packages"].items()))
    try:
        result["cpu"] = cpu_inventory()
        for line in cpu_lines(result["cpu"]):
            log(line)
    except Exception as exc:  # inventory is informational and must not fail the analysis
        result["cpu"] = {"pass": False, "error": error_name(exc)}
    # Disk config of the machine that ran, so a benchmark file records the storage it measured on.
    usage = shutil.disk_usage(work)
    result["disk"] = {"work_dir": str(work), "total_gib": round(usage.total / 2**30, 2),
                      "used_gib": round(usage.used / 2**30, 2), "free_gib": round(usage.free / 2**30, 2)}
    log(f"disk work_dir={work} total={result['disk']['total_gib']}GiB free={result['disk']['free_gib']}GiB")

    pipeline_start = time.perf_counter()
    pipeline_end = None
    data_ready = None
    try:
        objects, excluded = stage("list", lambda: list_source_objects(
            bucket, source, settings["skip_ion_image_chunks"]))
        download, skipped = split_download_objects(objects, source, settings["skip_ion_image_chunks"])
        download_bytes = sum(obj["size"] for obj in download)
        result["dataset"] = {"objects": len(objects), "bytes": sum(obj["size"] for obj in objects),
                             "planned_download_objects": len(download), "planned_download_bytes": download_bytes,
                             "downloaded_objects": None, "downloaded_bytes": None,
                             "skipped_ion_image_chunks": None if excluded else len(skipped),
                             "listing_excluded_prefixes": [f"{source}/ion_image/intensity/"] if excluded else [],
                             "total_objects_known": not excluded}
        log(f"dataset {result['source']} listed_objects={len(objects)} "
            f"listed_size={result['dataset']['bytes'] / 2**20:.1f}MiB "
            f"download={download_bytes / 2**20:.1f}MiB "
            f"excluded_ion_image_chunks={excluded}")
        local_source = work / Path(source).name
        check_disk_space(work, download_bytes)
        workers = settings.get("download_workers", DEFAULT_DOWNLOAD_WORKERS)
        log(f"download workers={workers}")
        with ThreadPoolExecutor(max_workers=1) as importer:
            imported = importer.submit(stage, "import", import_massflow)
            actual_download_bytes = stage("download", lambda: download_objects(
                bucket, download, source, local_source, workers))
            result["dataset"]["downloaded_objects"] = len(download)
            result["dataset"]["downloaded_bytes"] = actual_download_bytes
            before = snapshot_dir(local_source)
            imported.result()
        data_ready = time.perf_counter()
        umap = stage("umap", lambda: run_umap(settings, local_source, output / "umap_image.jpg"))
        result["umap"] = umap
        log(f"umap pixels={umap['pixels']} features={umap['features']} matrix={umap['matrix_mib']:.1f}MiB "
            f"fit_samples={umap['fit_samples']} sample_ratio={umap['sample_ratio']:.6f}")
        changed = diff_snapshot(before, snapshot_dir(local_source))
        result["result_files"] = changed
        log(f"upload {len(changed)} new zarr files -> {result['results']}")
        uploaded_bytes = stage("upload", lambda: upload_files(bucket, local_source, changed, results_prefix))
        pipeline_end = time.perf_counter()
        result["dataset"]["uploaded_bytes"] = uploaded_bytes
        result["throughput"] = throughput(result["stages_seconds"], actual_download_bytes, uploaded_bytes, umap)
        for line in throughput_lines(result["throughput"]):
            log(line)
        # Upload succeeded, so the local Zarr copy (downloaded dataset + UMAP output) is redundant;
        # remove it to free disk. Deliberately outside stage() so cleanup is not counted in the timings.
        shutil.rmtree(local_source, ignore_errors=True)
        log(f"cleaned local dataset copy {local_source} after successful upload")
        result["umap_pass"] = True
    except Exception as exc:
        pipeline_end = pipeline_end or time.perf_counter()
        result["error"] = error_detail(exc)
        log(f"umap pipeline FAILED: {result['error']}")
        if result["error"] != error_name(exc):  # OSS errors are reduced to class and code only
            for line in "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).splitlines():
                log(f"  {line}")
    result["timings"] = timing_summary(result["stages_seconds"], stage_intervals, pipeline_start,
                                       pipeline_end or time.perf_counter(), data_ready)
    timing = result["timings"]
    log("timings " + " ".join(f"{name}={value:.3f}s" for name, value in timing.items()
                            if isinstance(value, (int, float))))
    result["peak_rss_mib"] = round(peak_rss_mib(), 1)
    log(f"umap_pass={result['umap_pass']} peak_rss={result['peak_rss_mib']}MiB "
        f"stages_seconds={result['stages_seconds']}")

    # MassFlow logs to logs/ under the working directory, which main() points at the work dir.
    massflow_log = Path("logs") / "massflow.log"
    if massflow_log.is_file():
        shutil.copyfile(massflow_log, output / "massflow.log")
    (output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    log(f"archiving logs to {bucket_url}/{prefix}/")
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
                "oss_prefix": f"{bucket_url}/{prefix}/", "source": result["source"],
                "results": result["results"], "umap_pass": result["umap_pass"],
                "artifacts_uploaded": not upload_failed,
                "overall_pass": result["umap_pass"] and not upload_failed}
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
    return 3 if upload_failed else (0 if result["umap_pass"] else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # INSTANT limits argument length, so the submitter may split the JSON into several arguments.
    parser.add_argument("--settings-json", required=True, nargs="+", help="non-secret umap settings")
    args = parser.parse_args()
    try:
        settings = json.loads("".join(args.settings_json))
        validate_settings(settings, runtime=True)
        bucket = make_bucket(settings)
        work_root = Path(settings["work_dir"])
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="umap-", dir=work_root) as work, \
                tempfile.TemporaryDirectory(prefix="instant-results-") as output:
            os.chdir(work)  # MassFlow creates logs/ in the working directory on import
            return run(settings, bucket, Path(output), Path(work))
    except Exception as exc:
        print(f"job setup failed: {error_name(exc)}; check settings and OSS STS credentials", flush=True)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
