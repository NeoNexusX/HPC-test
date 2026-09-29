"""Reading run records straight from the OSS archive for the local UI."""
from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
import unittest
import zipfile

import support  # noqa: F401  (puts scripts/ on sys.path)
import ui_records


class FakeBucket:
    """The oss2.Bucket calls the UI makes: paged and delimited listings and GetObject."""
    bucket_name = "bucket"

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.gets = []
        self.listed = []

    def _info(self, key):
        data = self.objects[key]
        return SimpleNamespace(key=key, size=len(data), etag=hashlib.md5(data).hexdigest(), last_modified=1790000000)

    def list_objects_v2(self, prefix="", delimiter="", continuation_token="", max_keys=1000, **_):
        self.listed.append(prefix)
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        if delimiter:
            folders = sorted({prefix + key[len(prefix):].split(delimiter)[0] + delimiter
                              for key in keys if delimiter in key[len(prefix):]})
            return SimpleNamespace(object_list=[self._info(key) for key in keys if delimiter not in key[len(prefix):]],
                                   prefix_list=folders, is_truncated=False, next_continuation_token="")
        start = int(continuation_token or 0)
        end = start + max_keys
        return SimpleNamespace(object_list=[self._info(key) for key in keys[start:end]], prefix_list=[],
                               is_truncated=end < len(keys), next_continuation_token=str(end))

    def get_object(self, key):
        self.gets.append(key)
        return io.BytesIO(self.objects[key])


def manifest(run_id, passed=True):
    return json.dumps({"run_id": run_id, "attempt_id": "x", "job_id": f"job-{run_id}",
                       "source": "oss://bucket/bench/test_data/a.zarr/", "overall_pass": passed}).encode()


ARCHIVE = {
    "bench/run-1/att1/manifest.json": manifest("run-1"),
    "bench/run-1/att1/run.log": b"line 1\nline 2\n",
    "bench/run-1/att1/zarr-delta/analysis/zarr.json": b"{}",
    "bench/group-a/run-2/att2/manifest.json": manifest("run-2", passed=False),
    "bench/group-a/run-2/att2/result.json": b"{}",
    "bench/unfinished/att3/run.log": b"no manifest yet",
    "bench/test_data/a.zarr/zarr.json": b"{}",
    "bench/test_data/a.zarr/c/0/0": b"chunk",
}


class UiRecordsTests(unittest.TestCase):
    def test_scan_finds_finished_attempts_and_skips_datasets(self):
        bucket = FakeBucket(dict(ARCHIVE))
        manifests = {}
        records = {record["run_id"]: record for record in ui_records.scan(bucket, "bench", manifests)}
        self.assertEqual(set(records), {"run-1", "run-2"})  # no manifest.json: not archived yet
        self.assertEqual((records["run-1"]["group"], records["run-2"]["group"]), ("", "group-a"))
        self.assertEqual((records["run-1"]["status"], records["run-2"]["status"]), ("succeeded", "failed"))
        self.assertEqual([file["name"] for file in records["run-1"]["files"]],
                         ["manifest.json", "run.log", "zarr-delta/analysis/zarr.json"])
        self.assertEqual(records["run-2"]["id"], "bucket/bench/group-a/run-2/att2")
        self.assertEqual(records["run-1"]["finished_at"], "2026-09-21T14:13:20+00:00")
        self.assertFalse(any("test_data" in key for key in bucket.gets))
        # Manifests are uploaded last and never rewritten, so a second scan reads none again.
        reads = len(bucket.gets)
        ui_records.scan(bucket, "bench", manifests)
        self.assertEqual(len(bucket.gets), reads)

    def test_summarize_reads_current_and_older_result_layouts(self):
        current = ui_records.summarize({
            "cpu": {"summary": {"model": "Xeon 8369B", "cpus": "8"}}, "peak_rss_mib": 4153.3,
            "stages_seconds": {"download": 33.0, "umap": 118.6, "note": "skip"},
            "settings": {"benchmark_labels": {"instance_type": "ecs.c7.2xlarge"}}})
        self.assertEqual((current["cpu_model"], current["cpus"], current["total_seconds"]),
                         ("Xeon 8369B", "8", 151.6))
        self.assertEqual(current["labels"], {"instance_type": "ecs.c7.2xlarge"})
        overlapping = ui_records.summarize({"stages_seconds": {"list": 1.0, "import": 12.0,
                                                                 "download": 4.0, "umap": 10.0},
                                            "timings": {"pipeline_seconds": 23.0,
                                                        "massflow_import_seconds": 12.0}})
        self.assertEqual(overlapping["total_seconds"], 23.0)
        self.assertEqual(overlapping["timings"]["massflow_import_seconds"], 12.0)
        older = ui_records.summarize({"checks": {"cpu": {"lscpu": {"lscpu": [
            {"field": "CPU(s):", "data": "2"}, {"field": "Model name:", "data": "Xeon Platinum"}]}}}})
        self.assertEqual((older["cpu_model"], older["cpus"], older["total_seconds"]), ("Xeon Platinum", "2", None))

    def test_detail_and_zip_read_objects_from_the_record(self):
        bucket = FakeBucket(dict(ARCHIVE))
        record = next(r for r in ui_records.scan(bucket, "bench", {}) if r["run_id"] == "run-1")
        self.assertEqual(ui_records.detail(bucket, record), {"result": None, "log": ["line 1", "line 2"]})
        stream = io.BytesIO()
        ui_records.write_zip([(bucket, file["key"], file["key"][len("bench/"):]) for file in record["files"]], stream)
        archive = zipfile.ZipFile(stream)
        self.assertEqual(archive.read("run-1/att1/run.log"), b"line 1\nline 2\n")
        self.assertEqual(len(archive.namelist()), 3)


if __name__ == "__main__":
    unittest.main()
