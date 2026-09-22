"""Offline checks for the workstation-side Slurm trigger."""

from __future__ import annotations

import argparse
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trigger_epc_test as trigger


def options() -> argparse.Namespace:
    return argparse.Namespace(
        image="registry.example.com/team/test:sha-123",
        run_id="run-123",
        oss_uri="oss://bucket/results",
        epc_command="python -m epc_insta --version",
        remote_results_dir="/shared/results",
        remote_dir="/shared/epc-insta-test",
        shared_dir="/shared",
        ssd_dir="/mnt",
        require_shared=True,
        ssd_test=False,
        job_name="epc-insta-test",
        partition=None,
        account=None,
    )


class TriggerTest(unittest.TestCase):
    def test_exports_encode_commands_with_spaces(self) -> None:
        values = dict(field.split("=", 1) for field in trigger.build_export(options(), "run-123").split(","))
        command = base64.b64decode(values["EPC_INSTA_CMD_B64"]).decode("utf-8")
        self.assertEqual(command, "python -m epc_insta --version")
        self.assertEqual(values["REQUIRE_SHARED"], "1")

    @patch.object(trigger, "remote", return_value="12345;cluster\n")
    def test_submits_one_node_and_parses_job_id(self, remote: object) -> None:
        job_id = trigger.submit(options(), "run-123")
        self.assertEqual(job_id, "12345")
        command = remote.call_args.args[1]
        self.assertIn("--nodes=1", command)
        self.assertIn("--ntasks-per-node=1", command)
        self.assertIn("/shared/results/run-123/slurm-%j.out", command)

    @patch.object(trigger, "remote", side_effect=["", "COMPLETED\n", '{"overall_pass": true, "upload_status": "passed"}'])
    def test_wait_reads_final_manifest(self, remote: object) -> None:
        state, manifest = trigger.wait_for_job(options(), "12345", "run-123")
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(manifest["upload_status"], "passed")


if __name__ == "__main__":
    unittest.main()
