#!/usr/bin/env python3
"""Run several INSTANT UMAP configurations back to back: for each config, each instance type and
each repeat, submit one job, wait for it, then start the next. Every job in one invocation writes
under a single OSS batch folder, and each result.json records the CPU, disk and planned config.

Layout on OSS (one folder per batch):
    <oss_prefix>/<batch_id>/<run_id>/<attempt>/{result.json, run.log, umap_image.jpg, manifest.json, ...}
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import re
import sys

import submit_instant as submit

ROOT = Path(__file__).resolve().parents[1]
MAX_REPEAT = 100


def resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else ROOT / path


def validate_batch(spec: dict) -> None:
    if set(spec) - {"batch_name", "runs"}:
        raise ValueError("batch spec allows only batch_name and runs; compare batch.example.json")
    name = spec.get("batch_name", "batch")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
        raise ValueError("batch_name must be short and match [A-Za-z0-9_-]")
    runs = spec.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("batch spec needs a non-empty runs list")
    for run in runs:
        if not isinstance(run, dict) or set(run) - {"config", "instance_types", "repeat", "dataset", "resources"}:
            raise ValueError("each run allows config, instance_types, repeat, dataset, resources")
        if not isinstance(run.get("config"), str) or not run["config"]:
            raise ValueError("each run needs a config path")
        repeat = run.get("repeat", 1)
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= MAX_REPEAT:
            raise ValueError(f"repeat must be an integer between 1 and {MAX_REPEAT}")
        types = run.get("instance_types")
        if types is not None and (not isinstance(types, list) or not types
                                  or not all(isinstance(t, str) and t for t in types)):
            raise ValueError("instance_types, if given, must be a non-empty list of instance type strings")
        if "dataset" in run and not isinstance(run["dataset"], str):
            raise ValueError("dataset must be a string")
        resources = run.get("resources")
        if resources is not None:
            if not isinstance(resources, dict) or not resources or set(resources) - submit.RESOURCE_KEYS:
                raise ValueError("run resources allows cores, memory_gib, system_disk_gib")
            for key, value in resources.items():
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"run resources.{key} must be a positive integer")


def expand(spec: dict, batch_id: str) -> list[dict]:
    """Turn the batch spec into one cell per (config x instance type x repeat), each ready to submit."""
    batch_name = spec.get("batch_name", "batch")
    cells = []
    for run in spec["runs"]:
        config = json.loads(resolve(run["config"]).read_text())
        if isinstance(config.get("umap"), dict):
            submit.select_dataset(config, run.get("dataset"))
        config["resources"].update(run.get("resources") or {})
        submit.validate_config(config)  # fail fast before any job is submitted
        base_prefix = config["umap"]["oss_prefix"]
        types = run.get("instance_types") or config["resources"].get("instance_types") or [None]
        repeat = run.get("repeat", 1)
        for instance_type in types:
            for index in range(1, repeat + 1):
                cfg = copy.deepcopy(config)
                if instance_type is None:
                    cfg["resources"].pop("instance_types", None)
                else:
                    cfg["resources"]["instance_types"] = [instance_type]
                # Group every job of this invocation under one OSS batch folder.
                cfg["umap"]["oss_prefix"] = f"{base_prefix}/{batch_id}"
                # Self-describing benchmark labels; the container echoes them into result.json.
                cfg["umap"]["benchmark_labels"] = {
                    "batch_id": batch_id, "batch_name": batch_name,
                    "config_file": Path(run["config"]).name,
                    "instance_type": instance_type or "any",
                    "cores": cfg["resources"]["cores"], "memory_gib": cfg["resources"]["memory_gib"],
                    "system_disk_gib": cfg["resources"]["system_disk_gib"],
                    "repeat_index": index, "repeat_total": repeat,
                }
                label = re.sub(r"[^A-Za-z0-9]+", "-", instance_type or "any").strip("-")
                run_id = f"{label}-r{index}-{submit.uuid.uuid4().hex[:6]}"[:64]
                submit.validate_config(cfg)  # validate the pinned instance type and labels
                cells.append({"config": cfg, "run_id": run_id, "config_file": Path(run["config"]).name,
                              "instance_type": instance_type or "any", "repeat_index": index})
    return cells


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", default=str(ROOT / "config" / "batch.local.json"),
                        help="batch spec listing configs, instance types and repeats")
    parser.add_argument("--env-file", default=str(ROOT / ".env"),
                        help="local secrets file (AccessKey, ACR pull password); never committed")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan; no API calls")
    parser.add_argument("--wait-timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--batch-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.wait = True  # a batch must wait for each job before starting the next
    try:
        if args.wait_timeout < 1 or args.poll_seconds < 1:
            raise ValueError("timeouts must be positive")
        if args.batch_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.batch_id):
            raise ValueError("invalid batch id")
        submit.load_env_file(Path(args.env_file))
        spec = json.loads(resolve(args.batch).read_text())
        validate_batch(spec)
        batch_id = args.batch_id or submit.new_run_id()
        cells = expand(spec, batch_id)
    except Exception as exc:
        return submit.report_error("setup", exc)

    batch_name = spec.get("batch_name", "batch")
    print(f"batch {batch_name} id={batch_id} runs={len(cells)}"
          f"{' (dry-run)' if args.dry_run else ''}", flush=True)
    records = []
    out = ROOT / "work" / batch_id
    out.mkdir(parents=True, exist_ok=True)

    def save_progress(current: dict | None = None) -> None:
        progress = {"batch_name": batch_name, "batch_id": batch_id,
                    "total": len(cells), "completed": len(records), "runs": records,
                    "current": current}
        (out / "batch_progress.json").write_text(
            json.dumps(progress, indent=2, ensure_ascii=False) + "\n")

    save_progress()
    for position, cell in enumerate(cells, start=1):
        head = (f"[{position}/{len(cells)}] run_id={cell['run_id']} "
                f"type={cell['instance_type']} repeat={cell['repeat_index']}")
        if args.dry_run:
            bucket = cell["config"]["umap"]["oss_bucket"]
            print(f"{head} -> oss://{bucket}/{submit.run_prefix(cell['config'], cell['run_id'])}/", flush=True)
            records.append({**{k: cell[k] for k in ("run_id", "config_file", "instance_type", "repeat_index")},
                            "exit_code": None})
            save_progress()
            continue
        save_progress({"run_id": cell["run_id"], "config_file": cell["config_file"],
                       "instance_type": cell["instance_type"],
                       "repeat_index": cell["repeat_index"], "status": "running",
                       "exit_code": None})
        print(head + " ...", flush=True)
        code, summary = submit.submit_and_wait(cell["config"], cell["run_id"], args)
        print(f"{head} -> exit_code={code}", flush=True)
        records.append({"run_id": cell["run_id"], "config_file": cell["config_file"],
                        "instance_type": cell["instance_type"], "repeat_index": cell["repeat_index"],
                        # "any" here while instance_type names a type means the sold-out fallback ran
                        "submitted_instance_types": (summary or {}).get("instance_types"),
                        "exit_code": code, "job_id": (summary or {}).get("job_id"),
                        "oss_prefix": (summary or {}).get("oss_prefix"),
                        "results": (summary or {}).get("results")})
        save_progress()

    passed = sum(1 for r in records if r["exit_code"] == 0)
    batch_summary = {"batch_name": batch_name, "batch_id": batch_id,
                     "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "dry_run": args.dry_run, "total": len(records),
                     "passed": passed, "failed": len(records) - passed, "runs": records}
    (out / "batch_summary.json").write_text(json.dumps(batch_summary, indent=2, ensure_ascii=False) + "\n")
    print(f"batch {batch_name} done: {passed}/{len(records)} passed; "
          f"summary at {out / 'batch_summary.json'}", flush=True)
    return 0 if args.dry_run or passed == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
