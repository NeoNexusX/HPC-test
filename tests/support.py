"""Shared fixtures. Kept free of the submit SDK so the image can run test_umap_job.py alone."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import oss2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SOURCE = "benchmark/test_data/sample.zarr"


def config():
    return {"region": "cn-hangzhou", "image": "registry.example.com/team/umap:sha-abc",
            "private_registry": True, "vswitch_id": "vsw-example", "security_group_id": "sg-example",
            "oss_role_arn": "acs:ram::1234567890123456:role/oss-umap",
            "resources": {"cores": 2, "memory_gib": 4, "system_disk_gib": 40},
            "umap": {"oss_bucket": "test-bucket", "oss_region": "cn-hangzhou",
                     "oss_endpoint": "https://oss-cn-hangzhou-internal.aliyuncs.com",
                     "oss_prefix": "benchmark", "source_zarr_path": SOURCE, "write_back": False,
                     "work_dir": "/tmp/umap-work", "skip_ion_image_chunks": True,
                     "full_matrix_mib": 1024, "sample_matrix_mib": 1024, "max_fit_samples": 20000}}


def sts():
    return {"AccessKeyId": "STS.example", "AccessKeySecret": "private-secret",
            "SecurityToken": "t" * 800, "Expiration": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()}


def etag(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()


class Bucket:
    """In-memory stand-in for the oss2.Bucket calls the job makes."""

    def __init__(self, objects=None, fail=None):
        self.objects = dict(objects or {})
        self.fail = fail
        self.gets = []

    def list_objects_v2(self, prefix="", continuation_token="", max_keys=100, **_):
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = int(continuation_token or 0)
        end = start + max_keys
        # The listing API quotes ETags; GetObject results do not.
        return SimpleNamespace(
            object_list=[SimpleNamespace(key=key, size=len(self.objects[key]), etag=f'"{etag(self.objects[key])}"')
                         for key in keys[start:end]],
            prefix_list=[], is_truncated=end < len(keys), next_continuation_token=str(end))

    def get_object_to_file(self, key, filename, headers=None, **_):
        self.gets.append(key)
        data = self.objects[key]
        if (headers or {}).get("If-Match", f'"{etag(data)}"') != f'"{etag(data)}"':
            raise oss2.exceptions.PreconditionFailed(412, {}, b"", {"Code": "PreconditionFailed"})
        Path(filename).write_bytes(data)
        return SimpleNamespace(etag=etag(data))

    def put_object_from_file(self, key, filename, **_):
        if key.endswith(self.fail or "impossible-suffix"):
            raise RuntimeError("sensitive SDK payload")
        self.objects[key] = Path(filename).read_bytes()
