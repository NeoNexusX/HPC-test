"""Local UI safety properties that matter when editing credentials and launching jobs."""
from __future__ import annotations

import asyncio
import io
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import HTTPException

from support import config
from test_ui_records import FakeBucket
import ui_server as ui


def read_body(response) -> bytes:
    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])
    return asyncio.run(collect())


class UiServerTests(unittest.TestCase):
    def test_env_edit_displays_values_and_overrides_shell_for_new_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("# keep this note\nACR_PULL_PASSWORD=old\n")
            with patch.object(ui, "ENV_FILE", env_file), \
                    patch.dict(os.environ, {"ACR_PULL_PASSWORD": "shell-value"}):
                response = ui.put_env(ui.EnvUpdate(changes=[
                    ui.EnvChange(key="ACR_PULL_PASSWORD", value="new-secret"),
                    ui.EnvChange(key="DB_PASS", value="database-secret"),
                    ui.EnvChange(key="CUSTOM_WORKERS", value="8"),
                    ui.EnvChange(key="CUSTOM_TEXT", value='  say "go" \\ now  ')]))
                by_key = {entry["key"]: entry for entry in response["entries"]}
                self.assertEqual(by_key["ACR_PULL_PASSWORD"]["value"], "new-secret")
                self.assertEqual(by_key["ACR_PULL_PASSWORD"]["source"], "file")
                self.assertTrue(by_key["ACR_PULL_PASSWORD"]["present"])
                self.assertEqual(by_key["DB_PASS"]["value"], "database-secret")
                self.assertEqual(by_key["CUSTOM_WORKERS"]["value"], "8")
                self.assertEqual(by_key["CUSTOM_TEXT"]["value"], '  say "go" \\ now  ')
                self.assertNotIn("secret", by_key["DB_PASS"])  # local-only UI: values shown as-is
                self.assertEqual(ui._effective_env()["ACR_PULL_PASSWORD"], "new-secret")
                self.assertIn("# keep this note", env_file.read_text())
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                fallback = ui.put_env(ui.EnvUpdate(changes=[
                    ui.EnvChange(key="ACR_PULL_PASSWORD", value="")]))
                password = next(entry for entry in fallback["entries"]
                                if entry["key"] == "ACR_PULL_PASSWORD")
                self.assertEqual(password["source"], "process")
                self.assertTrue(password["present"])
                self.assertEqual(password["value"], "shell-value")
                self.assertEqual(ui._effective_env()["ACR_PULL_PASSWORD"], "shell-value")

    def test_profile_path_cannot_read_outside_config_dir(self):
        with self.assertRaises(HTTPException):
            ui._profile(".env")
        with self.assertRaises(HTTPException):
            ui._profile("config/batch.local.json")
        with self.assertRaises(HTTPException):
            ui._profile("config/oss-policy.example.json")
        self.assertEqual(ui._profile("config/instant.big.json").name, "instant.big.json")

    def test_preview_uses_current_config_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            instant = Path(directory) / "instant.local.json"
            instant.write_text(__import__("json").dumps(config()))
            with patch.object(ui, "CONFIG_DIR", Path(directory)), \
                    patch.object(ui, "INSTANT_FILE", instant), \
                    patch.object(ui, "ROOT", Path(directory)):
                result = ui.preview(ui.PreviewRequest(
                    mode="instant", config_path="instant.local.json"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["preview"]["dataset"], "benchmark/test_data/sample.zarr")
        self.assertEqual(result["preview"]["create_job_request"]["Tasks"][0]
                         ["TaskSpec"]["TaskExecutor"][0]["Container"]["ImageRegistryOptions"],
                         "<redacted>")

    def test_missing_batch_local_uses_existing_profile_not_stale_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            example = {"batch_name": "example", "runs": [
                {"config": "config/instant.big.json", "repeat": 2}]}
            (config_dir / "batch.example.json").write_text(json.dumps(example))
            with patch.object(ui, "ROOT", root), patch.object(ui, "CONFIG_DIR", config_dir), \
                    patch.object(ui, "INSTANT_FILE", config_dir / "instant.local.json"), \
                    patch.object(ui, "BATCH_FILE", config_dir / "batch.local.json"):
                result = ui.put_config(ui.ConfigUpdate(instant=config()))
                self.assertEqual(result["batch"]["runs"], [
                    {"config": "config/instant.local.json", "repeat": 1}])
                self.assertFalse(result["batch_saved"])
                self.assertEqual(result["profile_resources"]["config/instant.local.json"]["cores"],
                                 config()["resources"]["cores"])
                self.assertTrue((config_dir / "instant.local.json").is_file())
                self.assertFalse((config_dir / "batch.local.json").exists())
                before = (config_dir / "instant.local.json").read_text()
                revised = {"batch_name": "reordered", "runs": [
                    {"config": "config/instant.local.json", "repeat": 2}]}
                ui.put_config(ui.ConfigUpdate(batch=revised))
                self.assertEqual((config_dir / "instant.local.json").read_text(), before)
                self.assertEqual(json.loads((config_dir / "batch.local.json").read_text()), revised)
                with self.assertRaises(HTTPException):
                    ui.put_config(ui.ConfigUpdate())
                with self.assertRaises(HTTPException):
                    ui.put_config(ui.ConfigUpdate(batch=example))

    def test_status_lists_only_recent_ui_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            now = ui.dt.datetime.now(ui.dt.timezone.utc)
            states = {"live": {"status": "running"},
                      "just-done": {"status": "failed", "finished_at": now.isoformat()},
                      "old-done": {"status": "succeeded",
                                   "finished_at": (now - ui.dt.timedelta(hours=2)).isoformat()}}
            for name, state in states.items():
                (work / name).mkdir()
                (work / name / "ui_state.json").write_text(json.dumps(
                    {"mode": "instant", "created_at": now.isoformat(), **state}))
            (work / "cli-run").mkdir()  # submitted from a terminal: its result is read from OSS instead
            (work / "cli-run" / "submission.json").write_text(json.dumps({"job_id": "job-1"}))
            with patch.object(ui, "WORK_DIR", work):
                status = ui.get_status()
        self.assertEqual({run["id"] for run in status["runs"]}, {"live", "just-done"})
        self.assertEqual(status["active_count"], 1)

    def test_record_endpoints_stream_known_files_and_zip_groups(self):
        bucket = FakeBucket({"bench/g1/run-a/att1/manifest.json": b"{}", "bench/g1/run-a/att1/run.log": b"log",
                             "bench/run-b/att2/manifest.json": b"{}"})
        archive = {"bucket": "bucket", "prefix": "bench"}

        def record(folder, group):
            files = [{"name": key[len(folder):], "key": key, "size": len(data)}
                     for key, data in bucket.objects.items() if key.startswith(folder)]
            return {"id": f"bucket/{folder.rstrip('/')}", "bucket": "bucket", "prefix": "bench", "group": group,
                    "run_id": folder.split("/")[-3], "attempt": folder.split("/")[-2], "finished_at": "2026-09-28",
                    "files": files, "archive": archive}
        cache = {"at": 0.0, "error": None, "synced_at": None, "records": {
            r["id"]: r for r in (record("bench/g1/run-a/att1/", "g1"), record("bench/run-b/att2/", ""))}}
        with patch.object(ui, "_records", return_value=cache), patch.object(ui, "_oss_bucket", return_value=bucket), \
                patch.object(ui, "_archives", return_value=[archive]):
            listing = ui.get_records()
            self.assertNotIn("files", listing["records"][0])
            self.assertEqual(listing["groups"], [{"name": "", "count": 1}, {"name": "g1", "count": 1}])
            with self.assertRaises(HTTPException):
                ui.get_record_file("bucket/bench/g1/run-a/att1", "../../test_data/secret.zarr")
            response = ui.get_record_file("bucket/bench/g1/run-a/att1", "run.log", download=True)
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(read_body(response), b"log")
            zipped = read_body(ui.download_records(group="g1"))
        names = zipfile.ZipFile(io.BytesIO(zipped)).namelist()
        self.assertEqual(sorted(names), ["g1/run-a/att1/manifest.json", "g1/run-a/att1/run.log"])

    def test_deleted_profile_drops_out_and_batch_save_still_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "instant.local.json").write_text(json.dumps(config()))
            big = config_dir / "instant.big.json"
            big.write_text(json.dumps(config()))
            with patch.object(ui, "ROOT", root), patch.object(ui, "CONFIG_DIR", config_dir), \
                    patch.object(ui, "INSTANT_FILE", config_dir / "instant.local.json"), \
                    patch.object(ui, "BATCH_FILE", config_dir / "batch.local.json"):
                self.assertIn("config/instant.big.json", ui.get_config_files()["profiles"])
                big.unlink()
                files = ui.get_config_files()
                self.assertEqual(files["profiles"], ["config/instant.local.json"])
                self.assertNotIn("config/instant.big.json", files["profile_resources"])
                batch = {"batch_name": "b", "runs": [{"config": "config/instant.local.json"}]}
                result = ui.put_config(ui.ConfigUpdate(batch=batch, config_path="config/instant.big.json"))
                self.assertEqual(result["config_path"], "config/instant.local.json")
                self.assertEqual(result["batch"], batch)
                with self.assertRaises(HTTPException):
                    ui.put_config(ui.ConfigUpdate(batch={"runs": [{"config": "config/instant.big.json"}]}))

    def test_cloud_refresh_uses_region_saved_with_submission(self):
        response = subprocess.CompletedProcess([], 0, '{"status":"Succeed"}', "")
        with patch.object(ui, "_profile", side_effect=AssertionError("profile was reread")), \
                patch.object(ui, "_effective_env", return_value={}), \
                patch.object(ui.subprocess, "run", return_value=response) as run:
            cloud = ui._cloud_status({"job_id": "job-123", "region": "cn-hongkong",
                                      "config_path": "config/deleted.local.json"})
        self.assertEqual(cloud["status"], "Succeed")
        self.assertEqual(run.call_args.args[0][-2:], ["cn-hongkong", "job-123"])

    def test_batch_detail_includes_current_child_before_it_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            batch_dir = work / "batch-1"
            child_dir = work / "child-1"
            batch_dir.mkdir()
            child_dir.mkdir()
            (batch_dir / "ui_state.json").write_text(json.dumps({
                "mode": "batch", "status": "running", "job_id": "job-123",
                "cloud_status": "Running", "created_at": "2026-09-29T00:00:00Z"}))
            (batch_dir / "batch_progress.json").write_text(json.dumps({
                "total": 2, "completed": 1, "runs": [{"run_id": "child-0", "exit_code": 0}],
                "current": {"run_id": "child-1", "status": "running", "exit_code": None}}))
            (child_dir / "submission.json").write_text(json.dumps({
                "job_id": "job-123", "dataset": "oss://bucket/sample.zarr/"}))
            with patch.object(ui, "WORK_DIR", work):
                detail = ui.get_run("batch-1", refresh=False)
        self.assertEqual(len(detail["children"]), 2)
        self.assertEqual(detail["children"][-1]["job_id"], "job-123")
        self.assertEqual(detail["children"][-1]["status"], "Running")


if __name__ == "__main__":
    unittest.main()
