"""CPU inventory and file-based FIO/OSS benchmarks for the INSTANT container."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

FIO_SOURCE = "https://www.alibabacloud.com/help/en/ecs/user-guide/test-the-performance-of-block-storage-devices"
# Local-disk profiles: rw, block size, queue depth, job count.
PROFILES = {
    "seqwrite": ("write", "128k", 128, 1),
    "seqread": ("read", "128k", 128, 1),
    "randwrite": ("randwrite", "4k", 32, 4),
    "randread": ("randread", "4k", 32, 4),
    "randwrite_latency": ("randwrite", "4k", 1, 1),
    "randread_latency": ("randread", "4k", 1, 1),
    "write_latency": ("write", "4k", 1, 1),
    "read_latency": ("read", "4k", 1, 1),
}


def validate_settings(settings: dict, runtime: bool = False) -> None:
    fields = {"oss_bucket", "oss_region", "oss_endpoint", "oss_prefix", "oss_size_mib",
              "disk_dir", "fio_size_mib", "fio_runtime_seconds"}
    if runtime:
        fields.add("run_id")
    if set(settings) != fields:
        raise ValueError("benchmark fields must match instant.example.json exactly")
    for key in ("oss_bucket", "oss_region", "oss_prefix", "disk_dir"):
        value = settings[key]
        if not isinstance(value, str) or not value or any(c in value for c in "<>\n\r"):
            raise ValueError(f"invalid benchmark.{key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", settings["oss_bucket"]):
        raise ValueError("invalid OSS bucket name")
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+", settings["oss_endpoint"]):
        raise ValueError("oss_endpoint must be an HTTPS service endpoint")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", settings["oss_prefix"]):
        raise ValueError("oss_prefix must be a plain path without wildcards or traversal")
    path = Path(settings["disk_dir"])
    if not path.is_absolute() or str(path).startswith("/dev") or path == Path("/") or ".." in path.parts:
        raise ValueError("disk_dir must be an absolute dedicated directory, never a device or root")
    for key in ("oss_size_mib", "fio_size_mib", "fio_runtime_seconds"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    if settings["oss_size_mib"] > 4096:
        raise ValueError("single-object OSS benchmark supports at most 4096 MiB")
    if runtime and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings["run_id"]):
        raise ValueError("invalid run_id")


def command(argv: list[str], timeout: int = 30) -> dict:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "pass": proc.returncode == 0}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "pass": False, "error": type(exc).__name__}


LSCPU_FIELDS = {"Model name": "model", "Vendor ID": "vendor", "Architecture": "architecture",
                "CPU(s)": "cpus", "Socket(s)": "sockets", "Core(s) per socket": "cores_per_socket",
                "Thread(s) per core": "threads_per_core", "CPU max MHz": "max_mhz",
                "L3": "l3_cache", "Hypervisor vendor": "hypervisor"}


def _flatten(entries: list):
    for entry in entries:
        yield entry
        yield from _flatten(entry.get("children", []))


def lscpu_summary(lscpu: dict) -> dict:
    # util-linux >= 2.38 nests fields under "children"; older versions are flat.
    summary = {}
    for entry in _flatten(lscpu.get("lscpu", [])):
        key = LSCPU_FIELDS.get(entry.get("field", "").rstrip(":"))
        if key:
            summary.setdefault(key, entry.get("data"))
    return summary


def cpu_inventory() -> dict:
    result = command(["lscpu", "--json"])
    if result["pass"]:
        result["lscpu"] = json.loads(result.pop("stdout"))
        result["summary"] = lscpu_summary(result["lscpu"])
    result["os_cpu_count"] = os.cpu_count()
    result["affinity_cpu_ids"] = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    result["cgroup"] = {}
    for name in ("cpu.max", "cpuset.cpus.effective", "cpu/cpu.cfs_quota_us", "cpu/cpu.cfs_period_us"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result["cgroup"][name] = path.read_text().strip()
    result["note"] = "lscpu describes visible hardware; affinity/cgroup may further restrict job CPUs."
    return result


def fio_benchmark(settings: dict, output: Path) -> dict:
    directory = Path(settings["disk_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    if str(resolved).startswith("/dev") or resolved == Path("/"):
        raise ValueError("resolved disk directory is unsafe")
    result = {"path": str(resolved), "method": "file-based adaptation of Alibaba Cloud local-disk profiles",
              "source": FIO_SOURCE, "physical_local_ssd_verified": False,
              "runtime_seconds_per_profile": settings["fio_runtime_seconds"],
              "official_runtime_seconds": 1000,
              "filesystem": command(["findmnt", "--json", "--target", str(resolved)]),
              "block_devices": command(["lsblk", "--json", "-o", "NAME,TYPE,SIZE,ROTA,MODEL,MOUNTPOINTS"]),
              "profiles": {}, "pass": False}
    if not shutil.which("fio"):
        result["error"] = "fio is not installed"
        return result
    size = settings["fio_size_mib"] * 1024 * 1024
    if shutil.disk_usage(directory).free < size + 64 * 1024 * 1024:
        result["error"] = "insufficient disk space"
        return result
    runtime = settings["fio_runtime_seconds"]
    with tempfile.TemporaryDirectory(prefix="fio-", dir=directory) as work:
        target = Path(work) / "test.dat"
        # Materialize the whole file before any timed read test.
        prefill = command(["fio", "--name=prefill", f"--filename={target}", "--rw=write",
                           "--bs=1M", "--direct=1", "--ioengine=libaio", f"--size={size}",
                           "--iodepth=32", "--output-format=json"], max(300, runtime * 2))
        (output / "fio-prefill.json").write_text(prefill.pop("stdout", ""))
        result["prefill"] = prefill
        if not prefill["pass"]:
            return result
        for name, (rw, block, depth, jobs) in PROFILES.items():
            raw_path = output / f"fio-{name}.json"
            argv = ["fio", f"--name={name}", f"--filename={target}", "--direct=1", "--ioengine=libaio",
                    f"--rw={rw}", f"--bs={block}", f"--size={size}", f"--runtime={runtime}",
                    "--time_based=1", f"--iodepth={depth}", f"--numjobs={jobs}",
                    "--group_reporting=1", "--output-format=json"]
            record = command(argv, runtime + 120)
            raw_path.write_text(record.pop("stdout", ""))
            record["raw_file"] = raw_path.name
            if record["pass"]:
                try:
                    parsed = json.loads(raw_path.read_text())
                    record["pass"] = bool(parsed["jobs"]) and all(job.get("error", 0) == 0 for job in parsed["jobs"])
                    record["metrics"] = [{direction: {
                        "iops": job.get(direction, {}).get("iops"),
                        "bandwidth_bytes_per_second": job.get(direction, {}).get("bw_bytes"),
                        "completion_latency_ns": job.get(direction, {}).get("clat_ns"),
                    } for direction in ("read", "write")} for job in parsed["jobs"]]
                except (ValueError, KeyError, TypeError):
                    record["pass"] = False
                    record["error"] = "invalid fio JSON"
            result["profiles"][name] = record
    result["pass"] = all(record["pass"] for record in result["profiles"].values())
    return result


def error_name(exc: Exception) -> str:
    # Only the class and OSS error code: full SDK messages may embed signing details.
    code = getattr(exc, "code", None)
    name = type(exc).__name__
    return f"{name}:{code}" if isinstance(code, str) and code and code != name else name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def oss_benchmark(bucket, settings: dict, prefix: str) -> dict:
    size = settings["oss_size_mib"] * 1024 * 1024
    key = f"{prefix}/transfer-test.bin"
    result = {"pass": False, "bytes": size, "endpoint": settings["oss_endpoint"],
              "concurrency": 1, "method": "single PUT/GET, wall time includes SDK and local file I/O", "key": key}
    with tempfile.TemporaryDirectory(prefix="oss-", dir=settings["disk_dir"]) as work:
        source, downloaded = Path(work) / "source.bin", Path(work) / "download.bin"
        try:
            if shutil.disk_usage(work).free < size * 2 + 64 * 1024 * 1024:
                raise ValueError("insufficient transfer scratch space")
            with source.open("wb") as stream:
                for _ in range(settings["oss_size_mib"]):
                    stream.write(os.urandom(1024 * 1024))
            expected = sha256(source)
            for direction, action in (
                ("upload", lambda: bucket.put_object_from_file(key, str(source))),
                ("download", lambda: bucket.get_object_to_file(key, str(downloaded))),
            ):
                start = time.perf_counter()
                action()
                elapsed = time.perf_counter() - start
                result[direction] = {"seconds": elapsed, "mib_per_second": size / 1024**2 / elapsed}
            actual = sha256(downloaded)
            result.update(sha256=expected, downloaded_sha256=actual, **{"pass": expected == actual})
        except Exception as exc:
            result["error"] = error_name(exc)
        finally:
            try:
                bucket.delete_object(key)
                result["cleanup"] = "passed"
            except Exception as exc:
                result.update(cleanup="failed", cleanup_error=error_name(exc), **{"pass": False})
    return result


def _reason(record: dict) -> str:
    lines = (record.get("stderr") or "").strip().splitlines()
    return lines[-1][:300] if lines else str(record.get("error", f"returncode={record.get('returncode')}"))


def _us(nanoseconds) -> str:
    return "n/a" if nanoseconds is None else f"{nanoseconds / 1000:.1f}us"


def summarize(name: str, check: dict) -> list[str]:
    """Human-readable result lines for run.log; result.json keeps the full data."""
    if set(check) == {"pass", "error"}:  # the check itself raised
        return [f"{name} FAILED: {check.get('error')}"]
    if name == "cpu":
        s = check.get("summary", {})
        failed = [] if check.get("pass") else [f"cpu lscpu FAILED: {_reason(check)}"]
        return failed + [f"cpu model: {s.get('model')} ({s.get('vendor')}, {s.get('architecture')})",
                f"cpu topology: cpus={s.get('cpus')} sockets={s.get('sockets')} "
                f"cores/socket={s.get('cores_per_socket')} threads/core={s.get('threads_per_core')} "
                f"max_mhz={s.get('max_mhz')} l3={s.get('l3_cache')} hypervisor={s.get('hypervisor')}",
                f"cpu visible to job: os_cpu_count={check.get('os_cpu_count')} "
                f"affinity={check.get('affinity_cpu_ids')} cgroup={check.get('cgroup')}"]
    if name == "disk":
        try:
            fs = json.loads(check["filesystem"]["stdout"])["filesystems"][0]
        except (KeyError, IndexError, TypeError, ValueError):
            fs = {}
        lines = [f"disk target={check.get('path')} fstype={fs.get('fstype')} source={fs.get('source')} "
                 f"runtime={check.get('runtime_seconds_per_profile')}s/profile physical_local_ssd_verified=false"]
        if check.get("error"):
            lines.append(f"disk FAILED: {check['error']}")
        if check.get("prefill") and not check["prefill"].get("pass"):
            lines.append(f"fio prefill FAILED: {_reason(check['prefill'])}")
        for profile, record in check.get("profiles", {}).items():
            rw, block, depth, jobs = PROFILES[profile]
            label = f"fio {profile:<17} {rw:<9} bs={block:<4} iodepth={depth:<3} numjobs={jobs}"
            metrics = (record.get("metrics") or [{}])[0].get("read" if "read" in rw else "write", {})
            if not record.get("pass"):
                lines.append(f"{label} FAILED: {_reason(record)}")
                continue
            bw = (metrics.get("bandwidth_bytes_per_second") or 0) / 1024**2
            clat = metrics.get("completion_latency_ns") or {}
            lines.append(f"{label} bw={bw:.1f}MiB/s iops={metrics.get('iops') or 0:.0f} "
                         f"lat_mean={_us(clat.get('mean'))} "
                         f"lat_p99={_us(clat.get('percentile', {}).get('99.000000'))}")
        return lines
    if name == "oss":
        lines = [f"oss endpoint={check.get('endpoint')} object={check.get('bytes', 0) // 1024**2}MiB "
                 f"concurrency={check.get('concurrency')}"]
        for direction in ("upload", "download"):
            if direction in check:
                lines.append(f"oss {direction}: {check[direction]['mib_per_second']:.1f}MiB/s "
                             f"({check[direction]['seconds']:.2f}s)")
        match = check.get("sha256") is not None and check["sha256"] == check.get("downloaded_sha256")
        lines.append(f"oss sha256_match={match} cleanup={check.get('cleanup')} error={check.get('error')}")
        return lines
    return []
