"""UMAP pipeline tests. The image job runs this file with MassFlow installed (REQUIRE_MASSFLOW=1)."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import oss2

from support import SOURCE, Bucket, config
import run_instant_job as runner
import umap_job

HAVE_MASSFLOW = importlib.util.find_spec("massflow") is not None


def dataset():
    return {f"{SOURCE}/zarr.json": b"{}",
            f"{SOURCE}/spectra/intensity/zarr.json": b"{}",
            f"{SOURCE}/spectra/intensity/c/0": b"s" * 64,
            f"{SOURCE}/ion_image/intensity/zarr.json": b"{}",
            f"{SOURCE}/ion_image/intensity/c/0": b"i" * 64,
            f"{SOURCE}/ion_image/offsets/c/0": b"o" * 8,
            f"{SOURCE}/axes/mz/c/0": b"m" * 8}


def settings(work: Path, **overrides):
    return dict(config()["umap"], run_id="run-123", work_dir=str(work), **overrides)


def fake_umap(settings, local_source, image_path):
    """Stands in for MassFlow: writes what plot_umap_image(save_matrix="zarr") adds."""
    assert not (local_source / "ion_image/intensity/c/0").exists(), "ion_image chunks must be skipped"
    assert (local_source / "ion_image/intensity/zarr.json").exists(), "ion_image metadata must be kept"
    for rel in ("analysis/zarr.json", "analysis/umap/zarr.json", "analysis/umap/scaled_embedding/c/0/0"):
        (local_source / rel).parent.mkdir(parents=True, exist_ok=True)
        (local_source / rel).write_bytes(b"u")
    image_path.write_bytes(b"jpg")
    return {"pixels": 120, "features": 40, "matrix_mib": 0.02, "fit_samples": 120, "sample_ratio": 1.0}


class UmapJobTests(unittest.TestCase):
    def test_skips_only_ion_image_chunks(self):
        objects = umap_job.list_source_objects(Bucket(dataset()), SOURCE)
        kept, skipped = umap_job.split_download_objects(objects, SOURCE, True)
        self.assertEqual([o["key"] for o in skipped], [f"{SOURCE}/ion_image/intensity/c/0"])
        self.assertEqual(len(kept), len(objects) - 1)
        self.assertEqual(umap_job.split_download_objects(objects, SOURCE, False), (objects, []))
        no_spectra = [o for o in objects if "/spectra/" not in o["key"]]
        self.assertEqual(umap_job.split_download_objects(no_spectra, SOURCE, True), (no_spectra, []))

    def test_listing_rejects_missing_dataset_and_unmappable_keys(self):
        with self.assertRaises(FileNotFoundError):
            umap_job.list_source_objects(Bucket(dataset()), "benchmark/test_data/other.zarr")
        with self.assertRaises(ValueError):
            umap_job.list_source_objects(Bucket({f"{SOURCE}/a//b": b"x"}), SOURCE)
        # Directory placeholders from console uploads are ignored.
        objects = umap_job.list_source_objects(Bucket(dict(dataset(), **{f"{SOURCE}/axes/": b""})), SOURCE)
        self.assertEqual(len(objects), len(dataset()))

    def test_download_checks_etag_and_leaves_no_partial_files(self):
        bucket = Bucket(dataset())
        objects = umap_job.list_source_objects(bucket, SOURCE)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample.zarr"
            self.assertEqual(umap_job.download_objects(bucket, objects, SOURCE, target),
                             sum(len(v) for v in dataset().values()))
            self.assertEqual((target / "spectra/intensity/c/0").read_bytes(), b"s" * 64)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["sample.zarr"])
        bucket.objects[f"{SOURCE}/axes/mz/c/0"] = b"changed after listing"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample.zarr"
            with self.assertRaises(oss2.exceptions.PreconditionFailed):
                umap_job.download_objects(bucket, objects, SOURCE, target)
            self.assertFalse((target / "axes/mz/c/0").exists())
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["sample.zarr"])

    def test_sample_ratio_uses_full_matrix_or_budget(self):
        class Ms(list):
            shared_mz_list = [0.0] * 1000  # 4000 bytes per pixel

        class Dm:
            storage_mode = "continuous"
            ms = Ms([None] * 300_000)  # 1144 MiB as float32

        s = config()["umap"]
        full = umap_job.select_sample_ratio(Dm, dict(s, full_matrix_mib=2048))
        self.assertEqual((full["fit_samples"], full["sample_ratio"]), (300_000, 1.0))
        sampled = umap_job.select_sample_ratio(Dm, s)
        self.assertEqual(sampled["fit_samples"], 20_000)
        self.assertLessEqual(-(-300_000 * sampled["sample_ratio"] // 1), 20_000)
        by_memory = umap_job.select_sample_ratio(Dm, dict(s, sample_matrix_mib=16))
        self.assertEqual(by_memory["fit_samples"], 16 * 2**20 // 4000)
        Dm.storage_mode = "processed"
        with self.assertRaises(ValueError):
            umap_job.select_sample_ratio(Dm, s)

    def test_disk_check_names_the_config_to_raise(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(OSError) as ctx:
            umap_job.check_disk_space(Path(directory), 2**60)
        self.assertIn("system_disk_gib", str(ctx.exception))

    def test_lscpu_summary_handles_nested_and_flat_output(self):
        nested = {"lscpu": [
            {"field": "Architecture:", "data": "x86_64"},
            {"field": "Vendor ID:", "data": "AuthenticAMD", "children": [
                {"field": "Model name:", "data": "AMD EPYC 9T24 96-Core Processor", "children": [
                    {"field": "Thread(s) per core:", "data": "2"}, {"field": "Socket(s):", "data": "1"}]}]},
            {"field": "Caches (sum of all):", "children": [{"field": "L3:", "data": "32 MiB (1 instance)"}]}]}
        summary = umap_job.lscpu_summary(nested)
        self.assertEqual(summary["model"], "AMD EPYC 9T24 96-Core Processor")
        self.assertEqual(summary["threads_per_core"], "2")
        self.assertEqual(summary["l3_cache"], "32 MiB (1 instance)")
        self.assertEqual(umap_job.lscpu_summary({"lscpu": [{"field": "Model name:", "data": "X"}]}), {"model": "X"})


@patch.object(runner, "cpu_inventory", return_value={"pass": True, "summary": {"model": "Test CPU"}})
class RunnerTests(unittest.TestCase):
    def run_job(self, bucket, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "work").mkdir()
            code = runner.run(settings(base / "work", **overrides), bucket, base / "out", base / "work")
            return code, {name: (base / "out" / name).read_text()
                          for name in ("run.log", "result.json", "manifest.json")}

    @patch.object(runner, "run_umap", side_effect=fake_umap)
    def test_results_go_to_run_prefix_and_dataset_stays_read_only(self, *_):
        bucket = Bucket(dataset())
        code, files = self.run_job(bucket)
        self.assertEqual(code, 0, files["run.log"])
        manifest = json.loads(files["manifest.json"])
        self.assertTrue(manifest["overall_pass"])
        prefix = manifest["oss_prefix"].removeprefix("oss://test-bucket/")
        written = {k for k in bucket.objects if k not in dataset()}
        self.assertTrue(all(k.startswith(prefix) for k in written), written)
        self.assertIn(f"{prefix}zarr-delta/analysis/umap/scaled_embedding/c/0/0", written)
        self.assertEqual({f"{prefix}{name}" for name in ("run.log", "result.json", "umap_image.jpg",
                                                         "oss-upload.json", "manifest.json")},
                         {k for k in written if "/zarr-delta/" not in k})
        self.assertNotIn(f"{SOURCE}/ion_image/intensity/c/0", bucket.gets)
        self.assertIn("umap pixels=120 features=40", files["run.log"])
        self.assertIn("cpu model: Test CPU", files["run.log"])
        result = json.loads(files["result.json"])
        self.assertEqual(result["dataset"]["skipped_ion_image_chunks"], 1)
        self.assertEqual(set(result["stages_seconds"]), {"list", "download", "umap", "upload"})

    @patch.object(runner, "run_umap", side_effect=fake_umap)
    def test_write_back_stores_analysis_in_source_zarr(self, *_):
        bucket = Bucket(dataset())
        code, files = self.run_job(bucket, write_back=True)
        self.assertEqual(code, 0, files["run.log"])
        self.assertIn(f"{SOURCE}/analysis/umap/scaled_embedding/c/0/0", bucket.objects)
        self.assertFalse(any("/zarr-delta/" in k for k in bucket.objects))

    @patch.object(runner, "run_umap", side_effect=ValueError("UMAP requires continuous spectra"))
    def test_umap_failure_is_logged_with_traceback_and_archived(self, *_):
        bucket = Bucket(dataset())
        code, files = self.run_job(bucket)
        self.assertEqual(code, 2)
        self.assertIn("umap pipeline FAILED: ValueError: UMAP requires continuous spectra", files["run.log"])
        self.assertIn("Traceback", files["run.log"])
        manifest = json.loads(files["manifest.json"])
        self.assertEqual((manifest["umap_pass"], manifest["overall_pass"]), (False, False))
        self.assertTrue(any(k.endswith("/manifest.json") for k in bucket.objects))

    def test_oss_errors_keep_only_class_and_code(self, _):
        class DeniedBucket(Bucket):
            def list_objects_v2(self, **_):
                raise oss2.exceptions.AccessDenied(403, {}, b"", {"Code": "AccessDenied",
                                                                  "Message": "signature secret-token"})
        code, files = self.run_job(DeniedBucket(dataset()))
        self.assertEqual(code, 2)
        self.assertIn("umap pipeline FAILED: AccessDenied", files["run.log"])
        self.assertNotIn("secret-token", "".join(files.values()))

    @patch.object(runner, "run_umap", side_effect=fake_umap)
    def test_archive_failures_exit_3(self, *_):
        for fail in ("result.json", "manifest.json"):
            code, files = self.run_job(Bucket(dataset(), fail=fail))
            self.assertEqual(code, 3, fail)
            self.assertFalse(json.loads(files["manifest.json"])["overall_pass"])
            self.assertNotIn("sensitive", files["run.log"])


def write_synthetic_zarr(path: Path) -> Path:
    """A small MassFlow MSI Zarr with the same layout as the test data: ion_image plus spectra."""
    import numpy as np
    from massflow.msi_zarr.writer import MSIZarrWriter

    rng = np.random.default_rng(0)
    width, height, mz = 12, 10, np.linspace(100, 500, 40)
    writer = MSIZarrWriter(str(path), row_axis="ion", encoding="continuous", metadata=None,
                           width=width, height=height, include_spectra=True)
    for y in range(1, height + 1):
        for x in range(1, width + 1):
            # Two spatial regions with different spectra give UMAP some structure.
            writer.add_spectrum(rng.random(mz.size, dtype=np.float32) + (x > width // 2), (x, y, 1), mz)
    writer.finalize()
    writer.close()
    return path


@unittest.skipUnless(HAVE_MASSFLOW or os.environ.get("REQUIRE_MASSFLOW"), "MassFlow is installed in the image")
class MassFlowIntegrationTests(unittest.TestCase):
    def test_real_umap_through_the_job_runner(self):
        """UMAP_TEST_ZARR=/path/to/dataset.zarr runs a real dataset instead of the synthetic one."""
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            os.chdir(base)  # MassFlow creates logs/ in the working directory
            try:
                local = Path(os.environ.get("UMAP_TEST_ZARR") or write_synthetic_zarr(base / "synthetic.zarr"))
                bucket = Bucket({f"{SOURCE}/{p.relative_to(local).as_posix()}": p.read_bytes()
                                 for p in local.rglob("*") if p.is_file() and "analysis" not in p.parts})
                (base / "work").mkdir()
                code = runner.run(settings(base / "work"), bucket, base / "out", base / "work")
                log = (base / "out" / "run.log").read_text()
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 0, log)
        written = [k for k in bucket.objects if k.startswith("benchmark/run-123/")]
        self.assertTrue(any(k.endswith("/zarr-delta/analysis/umap/scaled_embedding/zarr.json") for k in written), written)
        self.assertTrue(any(k.endswith("/umap_image.jpg") for k in written), written)
        self.assertFalse(any("/ion_image/intensity/c/" in k for k in bucket.gets))
        print(f"\n{log}")


if __name__ == "__main__":
    unittest.main()
