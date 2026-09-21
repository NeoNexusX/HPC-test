#!/usr/bin/env python3
"""E-HPC/EPC Insta smoke test.

The baseline uses only the Python standard library. Optional SSD and OSS
checks use the fio and ossutil executables supplied by the runtime image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_command(command: str, timeout: int) -> dict:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-8000:],
            "duration_seconds": round(time.perf_counter() - started, 3),
            "pass": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "pass": False,
            "error": f"timeout after {timeout}s",
        }


def run_argv(argv: list[str], timeout: int) -> dict:
    """Run a trusted argument vector without a shell."""
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return {
            "command": argv,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-8000:],
            "duration_seconds": round(time.perf_counter() - started, 3),
            "pass": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": argv,
            "returncode": 124,
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "pass": False,
            "error": f"timeout after {timeout}s",
        }


def file_check(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"epc-insta-smoke {socket.gethostname()} {time.time_ns()}\n".encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "bytes": len(payload), "sha256": digest, "pass": True}


def parse_size(value: str) -> int:
    """Parse a small human-readable size such as 64M or 1G."""
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    normalized = value.strip().upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?B?)?", normalized)
    if not match:
        raise ValueError(f"invalid size: {value}")
    number, suffix = match.groups()
    suffix = suffix or "B"
    suffix = suffix.rstrip("B") or "B"
    if suffix not in units:
        raise ValueError(f"unsupported size suffix: {suffix}")
    amount = float(number)
    if amount <= 0:
        raise ValueError("size must be positive")
    return int(amount * units[suffix])


def write_payload(path: Path, size_bytes: int) -> str:
    digest = hashlib.sha256()
    chunk_size = 1024 * 1024
    remaining = size_bytes
    with path.open("wb") as stream:
        while remaining:
            chunk = os.urandom(min(chunk_size, remaining))
            stream.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def run_oss_test(
    oss_uri: str,
    size: str,
    timeout: int,
    ossutil_bin: str | None,
    keep_object: bool,
) -> dict:
    """Measure ossutil upload/download speed for one temporary object."""
    if not oss_uri.startswith("oss://") or any(char.isspace() for char in oss_uri):
        return {"pass": False, "error": "--oss-uri must be an oss:// URI without whitespace"}
    ossutil = ossutil_bin or shutil.which("ossutil")
    if not ossutil:
        return {"pass": False, "error": "ossutil is not installed or not in PATH"}
    try:
        size_bytes = parse_size(size)
    except ValueError as exc:
        return {"pass": False, "error": str(exc)}

    with tempfile.TemporaryDirectory(prefix="epc-oss-") as temp_dir:
        temp_root = Path(temp_dir)
        upload_file = temp_root / "payload.bin"
        download_file = temp_root / "download.bin"
        expected_sha256 = write_payload(upload_file, size_bytes)
        upload_started = time.perf_counter()
        upload = run_argv(
            [ossutil, "cp", str(upload_file), oss_uri, "--force"],
            timeout,
        )
        upload_elapsed = time.perf_counter() - upload_started
        if upload["pass"]:
            download_started = time.perf_counter()
            download = run_argv(
                [ossutil, "cp", oss_uri, str(download_file), "--force"],
                timeout,
            )
            download_elapsed = time.perf_counter() - download_started
        else:
            download = {"pass": False, "error": "upload failed; download skipped"}
            download_elapsed = 0.0

        cleanup = {"pass": True, "skipped": keep_object}
        if not keep_object:
            cleanup = run_argv([ossutil, "rm", oss_uri, "--force"], timeout)

        downloaded_size = download_file.stat().st_size if download_file.exists() else 0
        downloaded_sha256 = (
            hashlib.sha256(download_file.read_bytes()).hexdigest()
            if download_file.exists()
            else None
        )
        download["bytes"] = downloaded_size
        download["sha256"] = downloaded_sha256
        download["sha256_match"] = downloaded_sha256 == expected_sha256
        download["pass"] = bool(download.get("pass")) and download["sha256_match"]
        upload_speed = size_bytes / upload_elapsed if upload["pass"] and upload_elapsed else 0
        download_speed = downloaded_size / download_elapsed if download["pass"] and download_elapsed else 0
        return {
            "pass": upload["pass"] and download["pass"],
            "ossutil": ossutil,
            "uri": oss_uri,
            "bytes": size_bytes,
            "size": size,
            "sha256": expected_sha256,
            "upload": {
                **upload,
                "bytes": size_bytes,
                "speed_bytes_per_sec": round(upload_speed, 2),
                "speed_mbps": round(upload_speed / 1024**2, 2),
            },
            "download": {
                **download,
                "speed_bytes_per_sec": round(download_speed, 2),
                "speed_mbps": round(download_speed / 1024**2, 2),
            },
            "cleanup": cleanup,
            "keep_object": keep_object,
        }


def run_fio_test(ssd_dir: Path, size: str, runtime: int, timeout: int) -> dict:
    """Run safe file-based FIO profiles; never target a raw block device."""
    fio = shutil.which("fio")
    if not fio:
        return {"pass": False, "error": "fio is not installed or not in PATH"}
    if not ssd_dir.is_dir() or not os.access(ssd_dir, os.W_OK):
        return {"pass": False, "path": str(ssd_dir), "error": "SSD test directory is absent or not writable"}
    if str(ssd_dir).startswith(("/dev", "\\\\.\\")):
        return {"pass": False, "path": str(ssd_dir), "error": "raw devices are not accepted; use a mounted filesystem"}

    test_file = ssd_dir / f"fio-epc-insta-{socket.gethostname()}.dat"
    profiles = (
        ("seqread", "read", "1M", "1"),
        ("seqwrite", "write", "1M", "1"),
        ("randread", "randread", "4k", "4"),
        ("randwrite", "randwrite", "4k", "4"),
    )
    results = {}
    try:
        for name, rw, block_size, jobs in profiles:
            argv = [
                fio,
                f"--name={name}",
                f"--filename={test_file}",
                "--direct=1",
                "--ioengine=libaio",
                f"--rw={rw}",
                f"--bs={block_size}",
                f"--size={size}",
                f"--runtime={runtime}",
                "--time_based",
                "--iodepth=32",
                f"--numjobs={jobs}",
                "--group_reporting",
                "--output-format=json",
            ]
            result = run_argv(argv, timeout)
            if result["pass"]:
                try:
                    parsed = json.loads(result["stdout"])
                    job = parsed["jobs"][0]
                    read_stats = job.get("read", {})
                    write_stats = job.get("write", {})
                    result["metrics"] = {
                        "read_iops": read_stats.get("iops"),
                        "write_iops": write_stats.get("iops"),
                        "read_bw_bytes_per_sec": read_stats.get("bw_bytes"),
                        "write_bw_bytes_per_sec": write_stats.get("bw_bytes"),
                        "read_clat_ns": read_stats.get("clat_ns", {}).get("mean"),
                        "write_clat_ns": write_stats.get("clat_ns", {}).get("mean"),
                    }
                except (ValueError, KeyError, TypeError) as exc:
                    result["pass"] = False
                    result["error"] = f"unable to parse fio JSON: {exc}"
            results[name] = result
            if not result["pass"]:
                break
    finally:
        try:
            test_file.unlink(missing_ok=True)
        except OSError:
            pass
    failed = [name for name, value in results.items() if not value.get("pass")]
    return {
        "path": str(ssd_dir),
        "test_file": str(test_file),
        "size": size,
        "runtime_seconds": runtime,
        "fio": fio,
        "profiles": results,
        "pass": not failed and len(results) == len(profiles),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="smoke-result.json")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--ssd-test",
        action="store_true",
        help="run file-based FIO SSD profiles on --ssd-dir",
    )
    parser.add_argument("--ssd-dir", default=os.getenv("EPC_SSD_DIR", "/mnt"))
    parser.add_argument("--ssd-size", default=os.getenv("EPC_SSD_SIZE", "1G"))
    parser.add_argument("--ssd-runtime", type=int, default=int(os.getenv("EPC_SSD_RUNTIME", "30")))
    parser.add_argument("--oss-test", action="store_true", help="measure ossutil upload/download speed")
    parser.add_argument("--oss-uri", default=os.getenv("OSS_TEST_URI"))
    parser.add_argument("--oss-size", default=os.getenv("OSS_TEST_SIZE", "64M"))
    parser.add_argument("--ossutil-bin", default=os.getenv("OSSUTIL_BIN"))
    parser.add_argument("--oss-timeout", type=int, default=int(os.getenv("OSS_TEST_TIMEOUT", "900")))
    parser.add_argument("--oss-keep-object", action="store_true")
    args = parser.parse_args()

    checks: dict[str, object] = {}
    checks["runtime"] = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
        "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        "slurm_procid": os.getenv("SLURM_PROCID"),
    }

    with tempfile.TemporaryDirectory(prefix="epc-insta-") as temp_dir:
        temp_path = Path(temp_dir) / "write-check.bin"
        checks["local_io"] = file_check(temp_path)

    checks["tools"] = {
        name: shutil.which(name)
        for name in ("python", "docker", "podman", "fio", "ossutil", "srun", "sbatch")
    }

    if args.ssd_test:
        checks["ssd"] = run_fio_test(
            Path(args.ssd_dir),
            args.ssd_size,
            args.ssd_runtime,
            max(args.timeout, args.ssd_runtime * 6),
        )

    if args.oss_test:
        if not args.oss_uri:
            checks["oss"] = {"pass": False, "error": "--oss-uri or OSS_TEST_URI is required"}
        else:
            checks["oss"] = run_oss_test(
                args.oss_uri,
                args.oss_size,
                args.oss_timeout,
                args.ossutil_bin,
                args.oss_keep_object,
            )

    epc_command = os.getenv("EPC_INSTA_CMD")
    if epc_command:
        checks["epc_insta"] = run_command(epc_command, args.timeout)
    else:
        checks["epc_insta"] = {
            "pass": True,
            "skipped": True,
            "reason": "EPC_INSTA_CMD is not set; baseline checks only",
        }

    failed = [name for name, value in checks.items() if isinstance(value, dict) and value.get("pass") is False]
    result = {
        "schema_version": 1,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall_pass": not failed,
        "failed_checks": failed,
        "checks": checks,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
