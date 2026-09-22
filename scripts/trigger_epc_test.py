#!/usr/bin/env python3
"""Trigger an E-HPC test remotely from a developer workstation.

The workstation only needs SSH access to the E-HPC login node. The login node
submits ``run_epc_job.sh`` to Slurm; the compute node pulls the requested
image, runs it, and uploads the run directory to OSS.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path


def encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def ssh_base(args: argparse.Namespace) -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={args.ssh_timeout}"]
    if args.ssh_key:
        command.extend(["-i", str(Path(args.ssh_key).expanduser())])
    command.append(args.ssh_target)
    return command


def run_local(command: list[str], timeout: int | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required command is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {exc.timeout}s: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"command failed ({exc.returncode}): {' '.join(command)}\n{detail}") from exc
    return result.stdout.strip()


def remote(args: argparse.Namespace, command: str) -> str:
    return run_local(ssh_base(args) + [command], timeout=args.ssh_timeout + 30)


def sync_executor(args: argparse.Namespace, local_script: Path) -> None:
    remote_dir = shlex.quote(args.remote_dir)
    remote_script_dir = f"{args.remote_dir.rstrip('/')}/scripts"
    remote(
        args,
        f"mkdir -p {shlex.quote(remote_script_dir)} {shlex.quote(args.remote_results_dir)}",
    )
    destination = f"{args.ssh_target}:{remote_script_dir}/run_epc_job.sh"
    command = ["scp"]
    if args.ssh_key:
        command.extend(["-i", str(Path(args.ssh_key).expanduser())])
    command.extend([str(local_script), destination])
    run_local(command, timeout=args.ssh_timeout + 30)
    remote(args, f"chmod 0755 {remote_dir}/scripts/run_epc_job.sh")


def build_export(args: argparse.Namespace, run_id: str) -> str:
    values = {
        "TEST_IMAGE_B64": args.image,
        "RUN_ID_B64": run_id,
        "OSS_URI_B64": args.oss_uri,
        "EPC_INSTA_CMD_B64": args.epc_command,
        "RESULT_ROOT_B64": args.remote_results_dir,
        "SHARED_DIR_B64": args.shared_dir,
        "SSD_DIR_B64": args.ssd_dir,
    }
    if args.require_shared:
        values["REQUIRE_SHARED"] = "1"
    if args.ssd_test:
        values["SSD_TEST"] = "1"
        values["SSD_SIZE_B64"] = args.ssd_size
        values["SSD_RUNTIME"] = str(args.ssd_runtime)
    return ",".join(f"{key}={encoded(value) if key.endswith('_B64') else value}" for key, value in values.items())


def submit(args: argparse.Namespace, run_id: str) -> str:
    result_dir = f"{args.remote_results_dir.rstrip('/')}/{run_id}"
    export_values = build_export(args, run_id)
    sbatch_args = [
        "sbatch",
        "--parsable",
        f"--job-name={args.job_name}",
        "--nodes=1",
        "--ntasks-per-node=1",
        f"--output={result_dir}/slurm-%j.out",
        f"--export=ALL,{export_values}",
    ]
    if args.partition:
        sbatch_args.append(f"--partition={args.partition}")
    if args.account:
        sbatch_args.append(f"--account={args.account}")
    sbatch_args.append(f"{args.remote_dir.rstrip('/')}/scripts/run_epc_job.sh")
    command = f"mkdir -p {shlex.quote(result_dir)} && cd {shlex.quote(args.remote_dir)} && {shlex.join(sbatch_args)}"
    output = remote(args, command)
    job_id = output.splitlines()[-1].strip().split(";", 1)[0]
    if not job_id.isdigit():
        raise RuntimeError(f"unable to parse Slurm job id from: {output}")
    return job_id


def wait_for_job(args: argparse.Namespace, job_id: str, run_id: str) -> tuple[str, dict[str, object] | None]:
    print(f"submitted Slurm job {job_id}; waiting for completion", flush=True)
    while True:
        state = remote(
            args,
            f"squeue -h -j {shlex.quote(job_id)} -o %T | head -n 1",
        ).strip()
        if state:
            print(f"  state: {state}", flush=True)
            time.sleep(args.poll_seconds)
            continue
        accounting = remote(
            args,
            f"sacct -X -n -j {shlex.quote(job_id)} --format=State | head -n 1 || true",
        ).strip()
        state = accounting or "UNKNOWN (accounting unavailable)"
        manifest_path = f"{args.remote_results_dir.rstrip('/')}/{run_id}/manifest.json"
        manifest_text = remote(
            args,
            f"cat {shlex.quote(manifest_path)} 2>/dev/null || true",
        ).strip()
        if not manifest_text:
            return state, None
        try:
            return state, json.loads(manifest_text)
        except json.JSONDecodeError:
            return state, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-target", required=True, help="SSH target, for example user@login-node")
    parser.add_argument("--ssh-key", help="private key passed to ssh/scp")
    parser.add_argument("--ssh-timeout", type=int, default=30)
    parser.add_argument("--remote-dir", default="/shared/epc-insta-test")
    parser.add_argument("--remote-results-dir", default="/shared/results")
    parser.add_argument("--shared-dir", default="/shared")
    parser.add_argument("--image", required=True, help="container image reference, preferably a GHCR SHA tag")
    parser.add_argument("--oss-uri", required=True, help="OSS prefix, for example oss://bucket/results")
    parser.add_argument("--epc-command", default="", help="EPC command executed inside the container")
    parser.add_argument("--baseline-only", action="store_true", help="run environment checks without an EPC command")
    parser.add_argument("--partition")
    parser.add_argument("--account")
    parser.add_argument("--job-name", default="epc-insta-test")
    parser.add_argument("--no-require-shared", dest="require_shared", action="store_false")
    parser.set_defaults(require_shared=True)
    parser.add_argument("--ssd-test", action="store_true")
    parser.add_argument("--ssd-dir", default="/mnt")
    parser.add_argument("--ssd-size", default="1G")
    parser.add_argument("--ssd-runtime", type=int, default=30)
    parser.add_argument("--run-id", help="optional stable id; defaults to UTC timestamp plus random suffix")
    parser.add_argument("--no-sync", action="store_true", help="do not upload the executor script before submitting")
    parser.add_argument("--wait", action="store_true", help="wait for Slurm completion and print the final state")
    parser.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.oss_uri.startswith("oss://"):
        raise SystemExit("--oss-uri must start with oss://")
    if not args.oss_uri[6:].strip("/"):
        raise SystemExit("--oss-uri must include a bucket name")
    if not args.epc_command and not args.baseline_only:
        raise SystemExit("--epc-command is required unless --baseline-only is specified")
    if args.epc_command and args.baseline_only:
        raise SystemExit("--epc-command and --baseline-only cannot be combined")
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.run_id):
        raise SystemExit("--run-id may contain only letters, numbers, dot, underscore, and hyphen")
    if args.poll_seconds < 1:
        raise SystemExit("--poll-seconds must be positive")

    run_id = args.run_id or f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    executor = Path(__file__).with_name("run_epc_job.sh")
    try:
        if not args.no_sync:
            sync_executor(args, executor)
        job_id = submit(args, run_id)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"run_id={run_id}")
    print(f"job_id={job_id}")
    print(f"oss_uri={args.oss_uri.rstrip('/')}/{run_id}/")
    if args.wait:
        try:
            final_state, manifest = wait_for_job(args, job_id, run_id)
            print(f"final_state={final_state}")
            if not final_state.startswith("COMPLETED"):
                return 2
            if manifest:
                print(f"overall_pass={manifest.get('overall_pass')}")
                print(f"upload_status={manifest.get('upload_status')}")
                if manifest.get("upload_status") != "passed":
                    return 3
                if manifest.get("overall_pass") is not True:
                    return 2
            else:
                print("remote manifest.json is missing or invalid", file=sys.stderr)
                return 2
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
