from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark
import run_instant_job as runner
import submit_instant as submit
from alibabacloud_ehpcinstant20230701.models import CreateJobRequest


def config():
    return {"region": "cn-hangzhou", "image": "registry.example.com/team/benchmark:sha-abc",
            "private_registry": True, "vswitch_id": "vsw-example", "security_group_id": "sg-example",
            "oss_role_arn": "acs:ram::1234567890123456:role/oss-benchmark",
            "resources": {"cores": 2, "memory_gib": 4, "system_disk_gib": 40},
            "benchmark": {"oss_bucket": "test-bucket", "oss_region": "cn-hangzhou",
                          "oss_endpoint": "https://oss-cn-hangzhou-internal.aliyuncs.com",
                          "oss_prefix": "benchmark", "oss_size_mib": 1, "disk_dir": "/tmp/bench",
                          "fio_size_mib": 16, "fio_runtime_seconds": 1}}


def sts():
    return {"AccessKeyId": "STS.example", "AccessKeySecret": "private-secret",
            "SecurityToken": "t" * 800, "Expiration": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()}


class Bucket:
    def __init__(self, corrupt=False, fail=None):
        self.objects = {}
        self.corrupt = corrupt
        self.fail = fail

    def put_object_from_file(self, key, path):
        if key.endswith(self.fail or "impossible-suffix"):
            raise RuntimeError("sensitive SDK payload")
        self.objects[key] = Path(path).read_bytes()

    def get_object_to_file(self, key, path):
        Path(path).write_bytes(b"corrupt" if self.corrupt else self.objects[key])

    def delete_object(self, key):
        self.objects.pop(key, None)


class SubmitTests(unittest.TestCase):
    def test_sdk_preserves_payload_and_secrets_are_redacted(self):
        request = submit.build_request(config(), "run-123", sts(), registry_env={
            "ACR_PULL_USERNAME": "pull-user", "ACR_PULL_PASSWORD": "pull-secret"})
        model = CreateJobRequest().from_map(request)
        model.validate()
        self.assertEqual(request, model.to_map())
        c = request["Tasks"][0]["TaskSpec"]["TaskExecutor"][0]["Container"]
        self.assertEqual(c["Command"], ["python", "/app/run_instant_job.py"])
        env = {e["Name"]: e["Value"] for e in c["EnvironmentVars"]}
        self.assertEqual("".join(env[f"OSS_TOKEN_{i}"] for i in range(4)), sts()["SecurityToken"])
        self.assertTrue(all(len(e) <= 256 for e in env.values()))
        preview = json.dumps(submit.redacted(request))
        self.assertNotIn("private-secret", preview)
        self.assertNotIn("pull-secret", preview)
        self.assertNotIn("t" * 256, preview)

    def test_stale_credentials_rejected_before_submission(self):
        credentials = sts()
        credentials["Expiration"] = "2020-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            submit.credential_env(credentials, 100)
        credentials = sts()
        credentials["AccessKeyId"] = "long-term-key"
        with self.assertRaises(ValueError):
            submit.credential_env(credentials, 100)

    def test_config_rejects_placeholders_and_unsafe_disk(self):
        c = config()
        c["image"] = "<replace>"
        with self.assertRaises(ValueError):
            submit.validate_config(c)
        c = config()
        c["benchmark"]["oss_prefix"] = "bench/*"
        with self.assertRaises(ValueError):
            submit.validate_config(c)
        c = config()
        c["benchmark"]["disk_dir"] = "/dev/nvme0n1"
        with self.assertRaises(ValueError):
            submit.validate_config(c)

    def test_long_settings_are_split_into_short_args_and_rejoined(self):
        c = config()
        c["benchmark"].update(oss_bucket="b" * 63, oss_prefix="team/benchmarks/" + "p" * 80)
        request = submit.build_request(c, "run-123", registry_env={})
        args = request["Tasks"][0]["TaskSpec"]["TaskExecutor"][0]["Container"]["Arg"]
        self.assertGreater(len(args), 2)
        self.assertTrue(all(len(a) <= 256 for a in args))
        captured = {}
        with patch.object(sys, "argv", ["run_instant_job.py", *args]), \
             patch.object(runner, "make_bucket"), \
             patch.object(runner, "run", side_effect=lambda settings, *_: captured.update(settings) or 0):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(captured, dict(c["benchmark"], run_id="run-123"))

    def test_instance_types_are_optional_and_validated(self):
        request = submit.build_request(config(), "run-123", registry_env={})
        self.assertNotIn("InstanceTypes", request["Tasks"][0]["TaskSpec"]["Resource"])
        c = config()
        c["resources"]["instance_types"] = ["ecs.g7.large", "ecs.c7.large"]
        request = submit.build_request(c, "run-123", registry_env={})
        submit.sdk_model(request)
        self.assertEqual(request["Tasks"][0]["TaskSpec"]["Resource"]["InstanceTypes"], ["ecs.g7.large", "ecs.c7.large"])
        c["resources"]["instance_types"] = ["<type>"]
        with self.assertRaises(ValueError):
            submit.validate_config(c)

    def test_assume_role_scopes_oss_access_to_this_run(self):
        client, credentials = Mock(), sts()
        client.assume_role.return_value.body.credentials.to_map.return_value = credentials
        self.assertEqual(submit.assume_oss_role(client, config(), "run-123", 3600), credentials)
        request = client.assume_role.call_args.args[0]
        self.assertEqual(request.role_arn, config()["oss_role_arn"])
        self.assertEqual(request.duration_seconds, 3600)
        statement = json.loads(request.policy)["Statement"][0]
        self.assertEqual(statement["Resource"], ["acs:oss:*:*:test-bucket/benchmark/run-123/*"])
        self.assertEqual(set(statement["Action"]), {"oss:PutObject", "oss:GetObject", "oss:DeleteObject"})

    def test_config_requires_valid_oss_role(self):
        c = config()
        c["oss_role_arn"] = "acs:ram::<account-id>:role/<oss-benchmark-role>"
        with self.assertRaises(ValueError):
            submit.validate_config(c)

    def test_env_file_fills_missing_variables_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# comment\nexport ACR_PULL_USERNAME=NeoNexus\nACR_PULL_PASSWORD='p=w\"d'\n"
                            "ALIBABA_CLOUD_ACCESS_KEY_ID=from-file\nALIBABA_CLOUD_ACCESS_KEY_SECRET=\n")
            path.chmod(0o600)
            with patch.dict(os.environ, {"ALIBABA_CLOUD_ACCESS_KEY_ID": "from-shell"}, clear=True):
                submit.load_env_file(path)
                self.assertEqual(os.environ["ACR_PULL_USERNAME"], "NeoNexus")
                self.assertEqual(os.environ["ACR_PULL_PASSWORD"], 'p=w"d')
                self.assertEqual(os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"], "from-shell")
                self.assertNotIn("ALIBABA_CLOUD_ACCESS_KEY_SECRET", os.environ)

    def test_dry_run_does_not_require_runtime_secrets(self):
        request = submit.build_request(config(), "run-123", registry_env={})
        self.assertEqual(request["Tasks"][0]["TaskSpec"]["TaskExecutor"][0]["Container"]["EnvironmentVars"], [])

    def test_wait_terminal_statuses(self):
        for state, code in [("Succeed", 0), ("Succeeded", 0), ("Failed", 2), ("Expired", 2)]:
            client = Mock()
            client.get_job.return_value.body.to_map.return_value = {"JobInfo": {"Status": state}}
            self.assertEqual(submit.wait_for_job(client, "job-123", 10, 1), code)

    @patch.object(submit.time, "monotonic", side_effect=[0, 11])
    def test_wait_timeout_does_not_cancel(self, _):
        client = Mock()
        self.assertEqual(submit.wait_for_job(client, "job-123", 10, 1), 4)
        self.assertEqual(client.mock_calls, [])


class BenchmarkTests(unittest.TestCase):
    def test_oss_roundtrip_and_cleanup(self):
        for corrupt in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                settings = dict(config()["benchmark"], disk_dir=directory)
                bucket = Bucket(corrupt=corrupt)
                result = benchmark.oss_benchmark(bucket, settings, "test/run")
                self.assertEqual(result["pass"], not corrupt)
                self.assertIn("upload", result)
                self.assertIn("download", result)
                self.assertEqual(bucket.objects, {})

    def test_oss_failure_does_not_log_sensitive_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = dict(config()["benchmark"], disk_dir=directory)
            result = benchmark.oss_benchmark(Bucket(fail="transfer-test.bin"), settings, "test/run")
            self.assertFalse(result["pass"])
            self.assertNotIn("sensitive", json.dumps(result))

    @patch.object(benchmark.shutil, "which", return_value="/usr/bin/fio")
    def test_fio_preserves_large_json_and_cleans_test_files(self, _):
        calls = []
        def command(argv, timeout=30):
            calls.append(argv)
            payload = {"padding": "x" * 20000, "jobs": [{"error": 0, "read": {"iops": 42}}]}
            return {"pass": True, "stdout": json.dumps(payload), "returncode": 0}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            disk, out = base / "disk", base / "out"
            out.mkdir()
            with patch.object(benchmark, "command", side_effect=command):
                result = benchmark.fio_benchmark(dict(config()["benchmark"], disk_dir=str(disk)), out)
            self.assertTrue(result["pass"])
            self.assertEqual(len(result["profiles"]), 8)
            self.assertGreater((out / "fio-seqread.json").stat().st_size, 12000)
            self.assertEqual(list(disk.iterdir()), [])
            seq = next(c for c in calls if "--name=seqread" in c)
            self.assertIn("--iodepth=128", seq)
            self.assertIn("--bs=128k", seq)
            self.assertFalse(result["physical_local_ssd_verified"])

    def test_archive_success_and_failure_exit_codes(self):
        for fail, tests_pass, expected in [(None, True, 0), ("result.json", True, 3),
                                          ("manifest.json", True, 3), (None, False, 2)]:
            with tempfile.TemporaryDirectory() as directory:
                bucket = Bucket(fail=fail)
                with patch.object(runner, "cpu_inventory", return_value={"pass": tests_pass}), \
                     patch.object(runner, "fio_benchmark", return_value={"pass": True}), \
                     patch.object(runner, "oss_benchmark", return_value={"pass": True}):
                    code = runner.run(dict(config()["benchmark"], run_id="run-123"), bucket, Path(directory))
                self.assertEqual(code, expected)
                manifest = json.loads((Path(directory) / "manifest.json").read_text())
                self.assertEqual(manifest["overall_pass"], expected == 0)
                self.assertNotIn("private-secret", json.dumps(bucket.objects, default=str))

    def test_lscpu_summary_handles_nested_and_flat_output(self):
        nested = {"lscpu": [
            {"field": "Architecture:", "data": "x86_64"},
            {"field": "Vendor ID:", "data": "GenuineIntel", "children": [
                {"field": "Model name:", "data": "Intel(R) Xeon(R) Platinum 8369B CPU @ 2.70GHz", "children": [
                    {"field": "Thread(s) per core:", "data": "2"}, {"field": "Socket(s):", "data": "1"}]}]},
            {"field": "Caches (sum of all):", "children": [{"field": "L3:", "data": "48 MiB (1 instance)"}]}]}
        summary = benchmark.lscpu_summary(nested)
        self.assertEqual(summary["model"], "Intel(R) Xeon(R) Platinum 8369B CPU @ 2.70GHz")
        self.assertEqual(summary["threads_per_core"], "2")
        self.assertEqual(summary["l3_cache"], "48 MiB (1 instance)")
        flat = {"lscpu": [{"field": "Model name:", "data": "AMD EPYC 7763"}]}
        self.assertEqual(benchmark.lscpu_summary(flat), {"model": "AMD EPYC 7763"})

    def test_run_log_contains_readable_results(self):
        cpu = {"pass": True, "summary": {"model": "Intel Xeon Test", "cpus": "2"}, "os_cpu_count": 2}
        clat = {"mean": 250000.0, "percentile": {"99.000000": 900000}}
        disk = {"pass": False, "path": "/tmp/bench", "runtime_seconds_per_profile": 1,
                "filesystem": {"stdout": json.dumps({"filesystems": [{"fstype": "overlay", "source": "overlay"}]})},
                "profiles": {
                    "seqread": {"pass": True, "metrics": [{"read": {"iops": 800.4, "bandwidth_bytes_per_second": 100 * 1024**2,
                                                                     "completion_latency_ns": clat}, "write": {}}]},
                    "randwrite": {"pass": False, "returncode": 1, "stderr": "fio: pid=1, err=22/file:io_u.c\n"}}}
        oss = {"pass": True, "bytes": 1024**2, "endpoint": "https://oss", "concurrency": 1,
               "upload": {"seconds": 0.5, "mib_per_second": 2.0}, "download": {"seconds": 0.25, "mib_per_second": 4.0},
               "sha256": "a", "downloaded_sha256": "a", "cleanup": "passed"}
        with tempfile.TemporaryDirectory() as directory:
            bucket = Bucket()
            with patch.object(runner, "cpu_inventory", return_value=cpu), \
                 patch.object(runner, "fio_benchmark", return_value=disk), \
                 patch.object(runner, "oss_benchmark", return_value=oss):
                self.assertEqual(runner.run(dict(config()["benchmark"], run_id="run-123"), bucket, Path(directory)), 2)
            log = next(v for k, v in bucket.objects.items() if k.endswith("/run.log")).decode()
        self.assertIn("cpu model: Intel Xeon Test", log)
        self.assertIn("fstype=overlay", log)
        self.assertRegex(log, r"fio seqread .*bw=100.0MiB/s iops=800 lat_mean=250.0us lat_p99=900.0us")
        self.assertRegex(log, r"fio randwrite .*FAILED: fio: pid=1, err=22/file:io_u.c")
        self.assertIn("oss upload: 2.0MiB/s", log)
        self.assertIn("oss download: 4.0MiB/s", log)
        self.assertIn("sha256_match=True", log)

    def test_runtime_uses_sts_v4_and_reassembles_token(self):
        import oss2
        env = {item["Name"]: item["Value"] for item in submit.credential_env(sts(), 100)}
        with patch.dict(os.environ, env, clear=True), patch.object(oss2, "StsAuth") as auth, patch.object(oss2, "Bucket"):
            runner.make_bucket(config()["benchmark"])
            self.assertEqual(auth.call_args.args[2], sts()["SecurityToken"])
            self.assertEqual(auth.call_args.kwargs["auth_version"], "v4")
            self.assertIs(oss2.Bucket.call_args.kwargs["enable_crc"], False)


if __name__ == "__main__":
    unittest.main()
