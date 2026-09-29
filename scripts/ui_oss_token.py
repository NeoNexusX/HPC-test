#!/usr/bin/env python3
"""Print short-lived, read-only STS credentials for the OSS run archive, in an isolated process.

    ui_oss_token.py <region> <role_arn> <bucket> <prefix>

The local UI lists and downloads run records with these instead of holding the AccessKey itself.
The session policy only allows listing <prefix>/ and reading objects under it.
"""
from __future__ import annotations

import json
import sys

from alibabacloud_sts20150401.models import AssumeRoleRequest

import submit_instant as submit

DURATION_SECONDS = 3600


def read_only_policy(bucket: str, prefix: str) -> dict:
    return {"Version": "1", "Statement": [
        {"Effect": "Allow", "Action": ["oss:ListObjects"], "Resource": [f"acs:oss:*:*:{bucket}"],
         "Condition": {"StringLike": {"oss:Prefix": [f"{prefix}/*"]}}},
        {"Effect": "Allow", "Action": ["oss:GetObject"], "Resource": [f"acs:oss:*:*:{bucket}/{prefix}/*"]},
    ]}


def main() -> int:
    region, role_arn, bucket, prefix = sys.argv[1:5]
    response = submit.make_sts_client(region).assume_role(AssumeRoleRequest(
        role_arn=role_arn, role_session_name="ehpc-ui-records", duration_seconds=DURATION_SECONDS,
        policy=json.dumps(read_only_policy(bucket, prefix), separators=(",", ":"))))
    print(json.dumps(response.body.credentials.to_map()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
