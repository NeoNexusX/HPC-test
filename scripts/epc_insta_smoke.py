#!/usr/bin/env python3
"""E-HPC/EPC Insta smoke test.

The script intentionally has no third-party dependency so it can run on a
fresh E-HPC login/compute node or inside the accompanying image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
    parser.add_argument("--shared-dir", default=os.getenv("EPC_SHARED_DIR", "/shared"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--require-shared",
        action="store_true",
        help="fail when --shared-dir is absent/not writable (recommended on E-HPC)",
    )
    parser.add_argument(
        "--ssd-test",
        action="store_true",
        help="run file-based FIO SSD profiles on --ssd-dir",
    )
    parser.add_argument("--ssd-dir", default=os.getenv("EPC_SSD_DIR", "/mnt"))
    parser.add_argument("--ssd-size", default=os.getenv("EPC_SSD_SIZE", "1G"))
    parser.add_argument("--ssd-runtime", type=int, default=int(os.getenv("EPC_SSD_RUNTIME", "30")))
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

    shared = Path(args.shared_dir)
    if shared.is_dir() and os.access(shared, os.W_OK):
        checks["shared_io"] = file_check(shared / f"smoke-{socket.gethostname()}.txt")
    else:
        checks["shared_io"] = {
            "path": str(shared),
            "pass": not args.require_shared,
            "skipped": not args.require_shared,
            "error": "shared directory is absent or not writable",
        }

    checks["tools"] = {
        name: shutil.which(name)
        for name in ("python", "docker", "podman", "fio", "srun", "sbatch")
    }

    if args.ssd_test:
        checks["ssd"] = run_fio_test(
            Path(args.ssd_dir),
            args.ssd_size,
            args.ssd_runtime,
            max(args.timeout, args.ssd_runtime * 6),
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


