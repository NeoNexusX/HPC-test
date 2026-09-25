from __future__ import annotations
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import SOURCE, config, sts
import run_instant_job as runner
import submit_instant as submit
from alibabacloud_ehpcinstant20230701.models import CreateJobRequest


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


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

    def test_config_rejects_placeholders_and_unsafe_paths(self):
        for key, value in [("oss_prefix", "bench/*"), ("work_dir", "/dev/nvme0n1"),
                           ("source_zarr_path", "data/../other.zarr"), ("source_zarr_path", "data/sample"),
                           ("write_back", "false")]:
            c = config()
            c["umap"][key] = value
            with self.assertRaises(ValueError, msg=key):
                submit.validate_config(c)
        c = config()
        c["image"] = "<replace>"
        with self.assertRaises(ValueError):
            submit.validate_config(c)

    def test_dataset_override_accepts_console_forms(self):
        for dataset in ["other/set.zarr", "other/set.zarr/", "oss://test-bucket/other/set.zarr/"]:
            c = config()
            submit.select_dataset(c, dataset)
            submit.validate_config(c)
            self.assertEqual(c["umap"]["source_zarr_path"], "other/set.zarr")
        c = config()
        c["umap"]["source_zarr_path"] += "/"
        submit.select_dataset(c, None)
        self.assertEqual(c["umap"]["source_zarr_path"], SOURCE)
        with self.assertRaises(ValueError):
            submit.select_dataset(config(), "oss://another-bucket/other/set.zarr")

    def test_long_settings_are_split_into_short_args_and_rejoined(self):
        captured = {}
        with tempfile.TemporaryDirectory() as work:
            c = config()
            c["umap"].update(oss_bucket="b" * 63, oss_prefix="team/benchmarks/" + "p" * 80, work_dir=work)
            request = submit.build_request(c, "run-123", registry_env={})
            args = request["Tasks"][0]["TaskSpec"]["TaskExecutor"][0]["Container"]["Arg"]
            self.assertGreater(len(args), 2)
            self.assertTrue(all(len(a) <= 256 for a in args))
            with patch.object(sys, "argv", ["run_instant_job.py", *args]), \
                 patch.object(runner, "make_bucket"), patch.object(runner.os, "chdir"), \
                 patch.object(runner, "run", side_effect=lambda settings, *_: captured.update(settings) or 0):
                self.assertEqual(runner.main(), 0)
        self.assertEqual(captured, dict(c["umap"], run_id="run-123"))

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
        c = config()
        c["resources"]["fallback_any_type"] = "yes"
        with self.assertRaises(ValueError):
            submit.validate_config(c)

    def submit_main(self, c, client):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(c))
            argv = ["submit_instant.py", "--config", str(path), "--env-file", str(Path(tmp) / "missing.env")]
            with patch.object(sys, "argv", argv), patch.object(sys, "stdout", io.StringIO()), \
                 patch.object(sys, "stderr", io.StringIO()), \
                 patch.dict(os.environ, {"ACR_PULL_USERNAME": "pull-user", "ACR_PULL_PASSWORD": "pull-secret"}), \
                 patch.object(submit, "ROOT", Path(tmp)), patch.object(submit, "make_sts_client"), \
                 patch.object(submit, "assume_oss_role", return_value=sts()), \
                 patch.object(submit, "make_client", return_value=client):
                return submit.main()

    def test_sold_out_instance_types_fall_back_to_any_type_only_when_enabled(self):
        for fallback, code, calls in [(True, 0, 2), (False, 1, 1)]:
            c = config()
            c["resources"].update(instance_types=["ecs.c7nex.large"], fallback_any_type=fallback)
            client = Mock()
            client.create_job.side_effect = [ApiError("RecommendEmpty.InstanceTypeSoldOut"),
                                              Mock(**{"body.job_id": "job-2"})]
            self.assertEqual(self.submit_main(c, client), code)
            resources = [call.args[0].to_map()["Tasks"][0]["TaskSpec"]["Resource"]
                         for call in client.create_job.call_args_list]
            self.assertEqual(len(resources), calls)
            self.assertEqual(resources[0]["InstanceTypes"], ["ecs.c7nex.large"])
            if fallback:
                self.assertNotIn("InstanceTypes", resources[1])

    def test_other_create_job_errors_are_not_retried(self):
        c = config()
        c["resources"].update(instance_types=["ecs.c7nex.large"], fallback_any_type=True)
        client = Mock()
        client.create_job.side_effect = ApiError("QuotaExceeded.Resource")
        self.assertEqual(self.submit_main(c, client), 1)
        self.assertEqual(client.create_job.call_count, 1)

    def test_assume_role_reads_dataset_and_writes_only_this_run(self):
        client, credentials = Mock(), sts()
        client.assume_role.return_value.body.credentials.to_map.return_value = credentials
        self.assertEqual(submit.assume_oss_role(client, config(), "run-123", 3600), credentials)
        request = client.assume_role.call_args.args[0]
        self.assertEqual(request.role_arn, config()["oss_role_arn"])
        self.assertEqual(request.duration_seconds, 3600)
        grants = {s["Action"][0]: s for s in json.loads(request.policy)["Statement"]}
        self.assertEqual(set(grants), {"oss:ListObjects", "oss:GetObject", "oss:PutObject"})
        self.assertEqual(grants["oss:ListObjects"]["Resource"], ["acs:oss:*:*:test-bucket"])
        self.assertEqual(grants["oss:ListObjects"]["Condition"], {"StringLike": {"oss:Prefix": [f"{SOURCE}/*"]}})
        self.assertEqual(grants["oss:GetObject"]["Resource"], [f"acs:oss:*:*:test-bucket/{SOURCE}/*"])
        self.assertEqual(grants["oss:PutObject"]["Resource"], ["acs:oss:*:*:test-bucket/benchmark/run-123/*"])

    def test_write_back_may_write_only_the_dataset_analysis_group(self):
        c = config()
        c["umap"]["write_back"] = True
        put = next(s for s in submit.oss_session_policy(c, "run-123")["Statement"] if s["Action"] == ["oss:PutObject"])
        self.assertEqual(put["Resource"], ["acs:oss:*:*:test-bucket/benchmark/run-123/*",
                                           f"acs:oss:*:*:test-bucket/{SOURCE}/analysis/*"])

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

    def test_wait_prints_executor_failure_reason(self):
        client = Mock()
        client.get_job.return_value.body.to_map.return_value = {"JobInfo": {"Status": "Exception", "Tasks": [
            {"ExecutorStatus": [{"ArrayId": 0, "StatusReason": "InstanceTypeSoldOut"}]}]}}
        with patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(submit.wait_for_job(client, "job-123", 10, 1), 2)
        self.assertIn("executor 0: InstanceTypeSoldOut", err.getvalue())

    @patch.object(submit.time, "monotonic", side_effect=[0, 11])
    def test_wait_timeout_does_not_cancel(self, _):
        client = Mock()
        self.assertEqual(submit.wait_for_job(client, "job-123", 10, 1), 4)
        self.assertEqual(client.mock_calls, [])


class RuntimeCredentialTests(unittest.TestCase):
    def test_runtime_uses_sts_v4_and_reassembles_token(self):
        import oss2
        env = {item["Name"]: item["Value"] for item in submit.credential_env(sts(), 100)}
        with patch.dict(os.environ, env, clear=True), patch.object(oss2, "StsAuth") as auth, patch.object(oss2, "Bucket"):
            runner.make_bucket(config()["umap"])
            self.assertEqual(auth.call_args.args[2], sts()["SecurityToken"])
            self.assertEqual(auth.call_args.kwargs["auth_version"], "v4")
            self.assertNotIn("enable_crc", oss2.Bucket.call_args.kwargs)  # real data keeps CRC64 checks


if __name__ == "__main__":
    unittest.main()
