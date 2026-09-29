"""Batch runner tests: spec validation, the config x instance-type x repeat matrix, and that main
runs every cell in order and writes a batch summary. Reuses submit_instant, not the container job,
so it does not need the Linux-only `resource` module or MassFlow."""
from __future__ import annotations
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import config
import run_batch
import submit_instant as submit


def write_config(directory, **umap_overrides):
    c = config()
    # An OS-appropriate absolute work_dir so validate_config passes on Windows too (the container is Linux).
    c["umap"]["work_dir"] = str(Path(tempfile.gettempdir(), "umap-work"))
    c["umap"].update(umap_overrides)
    path = Path(directory) / "instant.local.json"
    path.write_text(json.dumps(c))
    return path


class BatchTests(unittest.TestCase):
    def test_validate_batch_rejects_malformed_specs(self):
        for bad in [{"runs": []}, {"runs": "x"}, {"unknown": 1, "runs": [{"config": "c"}]},
                    {"runs": [{"config": ""}]}, {"runs": [{"config": "c", "repeat": 0}]},
                    {"runs": [{"config": "c", "repeat": 1.5}]}, {"runs": [{"config": "c", "repeat": True}]},
                    {"runs": [{"config": "c", "instance_types": []}]},
                    {"runs": [{"config": "c", "resources": {"cores": 0}}]},
                    {"runs": [{"config": "c", "resources": {"gpu": 1}}]},
                    {"runs": [{"config": "c", "other": 1}]},
                    {"batch_name": "bad name", "runs": [{"config": "c"}]}]:
            with self.assertRaises(ValueError, msg=bad):
                run_batch.validate_batch(bad)
        run_batch.validate_batch({"batch_name": "ok", "runs": [{"config": "c", "repeat": 2}]})

    def test_expand_makes_one_cell_per_config_type_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            spec = {"batch_name": "bench", "runs": [
                {"config": str(path), "instance_types": ["ecs.c7.large", "ecs.g7.large"], "repeat": 2},
                {"config": str(path), "repeat": 1}]}
            cells = run_batch.expand(spec, "20260101T000000Z-abcdef12")
        self.assertEqual([c["instance_type"] for c in cells],
                         ["ecs.c7.large", "ecs.c7.large", "ecs.g7.large", "ecs.g7.large", "any"])
        for cell in cells:
            cfg = cell["config"]
            self.assertTrue(cfg["umap"]["oss_prefix"].endswith("/20260101T000000Z-abcdef12"))
            labels = cfg["umap"]["benchmark_labels"]
            self.assertEqual(labels["batch_id"], "20260101T000000Z-abcdef12")
            self.assertEqual(labels["instance_type"], cell["instance_type"])
            submit.validate_config(cfg)  # every pinned cell is a valid submission
        self.assertEqual(cells[0]["config"]["resources"]["instance_types"], ["ecs.c7.large"])
        self.assertNotIn("instance_types", cells[-1]["config"]["resources"])  # "any" self-selects

    def test_dataset_override_applies_per_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            cells = run_batch.expand({"runs": [{"config": str(path), "dataset": "other/set.zarr"}]},
                                     "20260101T000000Z-abcdef12")
        self.assertEqual(cells[0]["config"]["umap"]["source_zarr_path"], "other/set.zarr")

    def test_resource_override_applies_to_each_cell_and_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            cells = run_batch.expand({"runs": [{"config": str(path),
                "instance_types": ["ecs.c7.large"],
                "resources": {"cores": 2, "memory_gib": 4, "system_disk_gib": 80}}]},
                "20260101T000000Z-abcdef12")
        resources = cells[0]["config"]["resources"]
        self.assertEqual((resources["cores"], resources["memory_gib"], resources["system_disk_gib"]),
                         (2, 4, 80))
        self.assertEqual(cells[0]["config"]["umap"]["benchmark_labels"]["cores"], 2)

    def test_run_ids_are_unique_and_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            cells = run_batch.expand(
                {"runs": [{"config": str(path), "instance_types": ["ecs.c7.large"], "repeat": 5}]},
                "20260101T000000Z-abcdef12")
        run_ids = [c["run_id"] for c in cells]
        self.assertEqual(len(set(run_ids)), 5)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r) for r in run_ids))

    def test_main_runs_each_cell_in_order_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            spec_path = Path(directory) / "batch.json"
            spec_path.write_text(json.dumps({"batch_name": "bench", "runs": [
                {"config": str(path), "instance_types": ["ecs.c7.large", "ecs.g7.large"], "repeat": 1}]}))
            seen = []
            progress_seen = []

            def fake_submit(cfg, run_id, args, **_):
                seen.append((run_id, cfg["resources"]["instance_types"][0]))
                progress_path = next((Path(directory) / "work").glob("*/batch_progress.json"))
                progress_seen.append(json.loads(progress_path.read_text()))
                # first cell fails, second passes: the batch must continue and record both
                code = 0 if len(seen) == 2 else 2
                # the second cell's type sold out and fell back to any type
                submitted = "any" if len(seen) == 2 else [seen[-1][1]]
                return code, {"job_id": f"job-{len(seen)}", "oss_prefix": "oss://b/x/", "results": "oss://b/x/",
                              "instance_types": submitted}

            argv = ["run_batch.py", "--batch", str(spec_path), "--env-file", str(Path(directory) / "missing.env")]
            with patch.object(sys, "argv", argv), patch.object(run_batch, "ROOT", Path(directory)), \
                 patch.object(sys, "stdout", __import__("io").StringIO()), \
                 patch.object(submit, "submit_and_wait", side_effect=fake_submit):
                code = run_batch.main()
            self.assertEqual(code, 1)  # not every cell passed
            self.assertEqual([t for _, t in seen], ["ecs.c7.large", "ecs.g7.large"])
            self.assertEqual([p["current"]["run_id"] for p in progress_seen], [r for r, _ in seen])
            self.assertEqual([p["completed"] for p in progress_seen], [0, 1])
            summaries = list((Path(directory) / "work").glob("*/batch_summary.json"))
            self.assertEqual(len(summaries), 1)
            data = json.loads(summaries[0].read_text())
            self.assertEqual((data["total"], data["passed"], data["failed"]), (2, 1, 1))
            self.assertEqual([r["exit_code"] for r in data["runs"]], [2, 0])
            self.assertEqual([r["submitted_instance_types"] for r in data["runs"]], [["ecs.c7.large"], "any"])

    def test_dry_run_plans_without_submitting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            spec_path = Path(directory) / "batch.json"
            spec_path.write_text(json.dumps({"runs": [{"config": str(path), "repeat": 2}]}))
            argv = ["run_batch.py", "--batch", str(spec_path), "--dry-run",
                    "--env-file", str(Path(directory) / "missing.env")]
            with patch.object(sys, "argv", argv), patch.object(run_batch, "ROOT", Path(directory)), \
                 patch.object(sys, "stdout", __import__("io").StringIO()), \
                 patch.object(submit, "submit_and_wait", side_effect=AssertionError("must not submit")) as never:
                code = run_batch.main()
            self.assertEqual(code, 0)
            never.assert_not_called()


if __name__ == "__main__":
    unittest.main()
