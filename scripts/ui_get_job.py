#!/usr/bin/env python3
"""Fetch a small, non-secret INSTANT status document in an isolated process."""
from __future__ import annotations

import json
import sys

from alibabacloud_ehpcinstant20230701.models import GetJobRequest

import submit_instant as submit


def get_job(client, job_id: str) -> dict:
    try:
        response = client.get_job(GetJobRequest(job_id=job_id)).body.to_map()
    except Exception as exc:
        if getattr(exc, "code", None) != "JobNotFound":
            raise
        return {"status": "NotFound", "resource": None,
                "status_reason": "JobNotFound: INSTANT no longer has this job; its record was deleted"}
    info = response.get("JobInfo") or {}
    tasks = info.get("Tasks") or []
    reasons = [executor["StatusReason"]
               for task in tasks
               for executor in task.get("ExecutorStatus") or []
               if executor.get("StatusReason")]
    return {"status": info.get("Status", "Unknown"),
            "status_reason": "; ".join(reasons) or None,
            "resource": (tasks[0].get("TaskSpec") or {}).get("Resource") if tasks else None}


def main() -> int:
    region, job_id = sys.argv[1:3]
    print(json.dumps(get_job(submit.make_client(region), job_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
