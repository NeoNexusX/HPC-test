#!/usr/bin/env python3
"""Submit or inspect an E-HPC INSTANT benchmark job over HTTPS. No SSH required."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUCCESS = {"Succeed", "Succeeded"}  # Both forms occur in the official documentation.
TERMINAL = SUCCESS | {"Failed", "Exception", "Expired", "Deleted"}
# Container.Command items and env values are documented as <= 256 chars; Arg's limit is unstated.
ARG_CHUNK = 256
RESOURCE_KEYS = {"cores", "memory_gib", "system_disk_gib"}


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into the environment; variables already set in the shell win."""
    if not path.is_file():
        return
    if path.stat().st_mode & 0o077:
        print(f"Warning: {path} is readable by other users; run: chmod 600 {path}", file=sys.stderr)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.removeprefix("export ").split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:  # an empty placeholder must not mask other credential sources
            os.environ.setdefault(key, value)


def validate_config(config: dict) -> None:
    required = {"region", "image", "vswitch_id", "security_group_id", "oss_role_arn", "resources", "benchmark"}
    allowed = required | {"enable_external_ip", "private_registry"}
    if required - config.keys() or config.keys() - allowed:
        raise ValueError("config has missing or unknown fields; compare instant.example.json")
    for key in ("region", "image", "vswitch_id", "security_group_id", "oss_role_arn"):
        value = config[key]
        if not isinstance(value, str) or not value or any(c in value for c in "<>\n\r"):
            raise ValueError(f"replace the placeholder for {key}")
    if not re.fullmatch(r"[a-z0-9-]+", config["region"]):
        raise ValueError("invalid region")
    if not re.fullmatch(r"acs:ram::\d+:role/[A-Za-z0-9.-]+", config["oss_role_arn"]):
        raise ValueError("oss_role_arn must look like acs:ram::<account-id>:role/<role-name>")
    resources = config["resources"]
    if RESOURCE_KEYS - resources.keys() or resources.keys() - RESOURCE_KEYS - {"instance_types"}:
        raise ValueError("resources requires cores, memory_gib, system_disk_gib (instance_types is optional)")
    for key in RESOURCE_KEYS:
        value = resources[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"resources.{key} must be positive")
    if not isinstance(resources["system_disk_gib"], int):
        raise ValueError("system_disk_gib must be an integer")
    types = resources.get("instance_types", [])
    if not isinstance(types, list) or len(types) > 5 or not all(
            isinstance(t, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]+", t) for t in types):
        raise ValueError("resources.instance_types must list at most 5 instance types, e.g. ecs.g7.large")
    for key in ("enable_external_ip", "private_registry"):
        if key in config and not isinstance(config[key], bool):
            raise ValueError(f"{key} must be boolean")
    from benchmark import validate_settings
    validate_settings(config["benchmark"])


def run_prefix(config: dict, run_id: str) -> str:
    return f"{config['benchmark']['oss_prefix'].strip('/')}/{run_id}"


def oss_session_policy(config: dict, run_id: str) -> dict:
    # Intersected with the role's own policy: the container can only touch this run's objects.
    resource = f"acs:oss:*:*:{config['benchmark']['oss_bucket']}/{run_prefix(config, run_id)}/*"
    return {"Version": "1", "Statement": [{
        "Effect": "Allow", "Action": ["oss:PutObject", "oss:GetObject", "oss:DeleteObject"],
        "Resource": [resource]}]}


def credential_env(credentials: dict, minimum_seconds: int) -> list[dict]:
    credentials = credentials.get("Credentials", credentials)
    for key in ("AccessKeyId", "AccessKeySecret", "SecurityToken", "Expiration"):
        if not isinstance(credentials.get(key), str) or not credentials[key]:
            raise ValueError(f"OSS STS credentials are missing {key}")
    if not credentials["AccessKeyId"].startswith("STS."):
        raise ValueError("OSS runtime credentials must be temporary STS credentials")
    expiration = dt.datetime.fromisoformat(credentials["Expiration"].replace("Z", "+00:00"))
    if expiration.tzinfo is None or (expiration - dt.datetime.now(dt.timezone.utc)).total_seconds() < minimum_seconds:
        raise ValueError("OSS STS validity is shorter than the configured queue/run budget")
    pairs = {
        "OSS_ACCESS_KEY_ID": credentials["AccessKeyId"],
        "OSS_ACCESS_KEY_SECRET": credentials["AccessKeySecret"],
        "OSS_STS_EXPIRATION": credentials["Expiration"],
    }
    token = credentials["SecurityToken"]
    for index, start in enumerate(range(0, len(token), 256)):
        pairs[f"OSS_TOKEN_{index}"] = token[start:start + 256]
    if len(pairs) > 20 or any(len(value) > 256 for value in pairs.values()):
        raise ValueError("STS credentials exceed INSTANT environment variable limits")
    return [{"Name": key, "Value": value} for key, value in pairs.items()]


def build_request(config: dict, run_id: str, credentials: dict | None = None,
                  minimum_seconds: int = 1800, registry_env=None) -> dict:
    validate_config(config)
    settings = json.dumps(dict(config["benchmark"], run_id=run_id), separators=(",", ":"))
    chunks = [settings[i:i + ARG_CHUNK] for i in range(0, len(settings), ARG_CHUNK)]
    if len(chunks) > 9:  # Arg allows at most 10 items including the flag.
        raise ValueError("benchmark settings are too long for INSTANT container arguments")
    container = {
        "Image": config["image"],
        "Command": ["python", "/app/run_instant_job.py"],
        "Arg": ["--settings-json", *chunks],
        "EnvironmentVars": credential_env(credentials, minimum_seconds) if credentials else [],
    }
    if config.get("private_registry", True):
        env = os.environ if registry_env is None else registry_env
        user, password = env.get("ACR_PULL_USERNAME"), env.get("ACR_PULL_PASSWORD")
        if credentials is not None and (not user or not password):
            raise ValueError("private image requires ACR_PULL_USERNAME and ACR_PULL_PASSWORD")
        container["ImageRegistryOptions"] = json.dumps({
            "ImageRegistryType": "https",
            "ImageRegistryServer": config["image"].split("/")[0],
            "ImageRegistryUserName": user or "<from environment>",
            "ImageRegistryPassword": password or "<from environment>",
        })
    r = config["resources"]
    resource = {"Cores": r["cores"], "Memory": r["memory_gib"],
                "Disks": [{"Type": "System", "Size": r["system_disk_gib"]}]}
    if r.get("instance_types"):
        resource["InstanceTypes"] = r["instance_types"]
    return {
        "JobName": f"benchmark-{run_id}",
        "JobDescription": "CPU inventory, file FIO, OSS transfer and result archive",
        "Tasks": [{
            "TaskName": "benchmark",
            "TaskSustainable": False,
            "ExecutorPolicy": {"MaxCount": 1, "ArraySpec": {"IndexStart": 0, "IndexEnd": 0, "IndexStep": 1}},
            "TaskSpec": {
                "TaskExecutor": [{"Container": container}],
                "VolumeMount": [],
                "Resource": resource,
                "RetryPolicy": {"RetryCount": 1, "ExitCodeActions": [
                    {"ExitCode": code, "Action": "Exit"} for code in (1, 2, 3, 64)
                ]},
            },
        }],
        "DeploymentPolicy": {"AllocationSpec": "Standard", "Tag": [], "Network": {
            "Vswitch": [config["vswitch_id"]],
            "EnableExternalIpAddress": config.get("enable_external_ip", False),
        }},
        "SecurityPolicy": {"SecurityGroup": {"SecurityGroupIds": [config["security_group_id"]]}},
    }


def sdk_model(request: dict):
    # Validate against the official SDK model even for a preview.
    from alibabacloud_ehpcinstant20230701.models import CreateJobRequest
    model = CreateJobRequest().from_map(request)
    model.validate()
    if model.to_map() != request:
        raise ValueError("SDK dropped an unsupported request field")
    return model


def redacted(request: dict) -> dict:
    result = copy.deepcopy(request)
    for task in result["Tasks"]:
        for executor in task["TaskSpec"]["TaskExecutor"]:
            container = executor["Container"]
            for value in container["EnvironmentVars"]:
                value["Value"] = "<redacted>"
            if "ImageRegistryOptions" in container:
                container["ImageRegistryOptions"] = "<redacted>"
    return result


def openapi_config(region: str, endpoint: str):
    # Local identity comes from the default credential chain (env vars, ~/.alibabacloud, CLI profile).
    from alibabacloud_credentials.client import Client as Credentials
    from alibabacloud_tea_openapi.models import Config
    return Config(credential=Credentials(), region_id=region, endpoint=endpoint, protocol="HTTPS",
                  connect_timeout=10000, read_timeout=30000)


def make_client(region: str):
    from alibabacloud_ehpcinstant20230701.client import Client
    return Client(openapi_config(region, f"ehpcinstant.{region}.aliyuncs.com"))


def make_sts_client(region: str):
    from alibabacloud_sts20150401.client import Client
    return Client(openapi_config(region, "sts.aliyuncs.com"))


def assume_oss_role(sts_client, config: dict, run_id: str, duration_seconds: int) -> dict:
    from alibabacloud_sts20150401.models import AssumeRoleRequest
    response = sts_client.assume_role(AssumeRoleRequest(
        role_arn=config["oss_role_arn"], role_session_name=f"ehpc-benchmark-{run_id}"[:64],
        duration_seconds=duration_seconds,
        policy=json.dumps(oss_session_policy(config, run_id), separators=(",", ":"))))
    return response.body.credentials.to_map()


def wait_for_job(client, job_id: str, timeout: int, interval: int) -> int:
    from alibabacloud_ehpcinstant20230701.models import GetJobRequest
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        response = client.get_job(GetJobRequest(job_id=job_id)).body.to_map()
        state = response.get("JobInfo", {}).get("Status", "Unknown")
        if state != previous:
            print(f"job_id={job_id} state={state}", flush=True)
            previous = state
        if state in TERMINAL:
            return 0 if state in SUCCESS else 2
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    print(f"Local wait timed out. Job {job_id} is NOT cancelled; inspect it in INSTANT console.", file=sys.stderr)
    return 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config" / "instant.local.json"))
    parser.add_argument("--env-file", default=str(ROOT / ".env"),
                        help="local secrets file (AccessKey, ACR pull password); never committed")
    parser.add_argument("--dry-run", action="store_true", help="validate and preview without credentials or API calls")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--job-id", help="wait for an existing job; never resubmit")
    parser.add_argument("--wait-timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    stage = "setup"
    try:
        if args.wait_timeout < 1 or args.poll_seconds < 1:
            raise ValueError("timeouts must be positive")
        if args.job_id and args.dry_run:
            raise ValueError("--job-id cannot be combined with --dry-run")
        load_env_file(Path(args.env_file))
        config = json.loads(Path(args.config).read_text())
        validate_config(config)
        if args.job_id:
            stage = "GetJob"
            return wait_for_job(make_client(config["region"]), args.job_id, args.wait_timeout, args.poll_seconds)
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        if args.dry_run:
            request = build_request(config, run_id, registry_env={})
            sdk_model(request)
            print(json.dumps({"create_job_request": redacted(request),
                              "oss_sts_session_policy": oss_session_policy(config, run_id)},
                             indent=2, ensure_ascii=False))
            return 0
        stage = "AssumeRole"
        # STS must outlive queueing + image pull + tests + archive; the local wait is the proxy budget.
        credentials = assume_oss_role(make_sts_client(config["region"]), config, run_id,
                                      max(3600, args.wait_timeout + 900))
        stage = "CreateJob"
        model = sdk_model(build_request(config, run_id, credentials, args.wait_timeout + 300))
        client = make_client(config["region"])
        # Do not automatically retry CreateJob: the API has no ClientToken field.
        job_id = client.create_job(model).body.job_id
        if not job_id:
            raise RuntimeError("CreateJob returned no JobId")
        summary = {"run_id": run_id, "job_id": job_id, "image": config["image"],
                   "oss_prefix": f"oss://{config['benchmark']['oss_bucket']}/{run_prefix(config, run_id)}/"}
        out = ROOT / "work" / run_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "submission.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2), flush=True)
        stage = "GetJob"
        return wait_for_job(client, job_id, args.wait_timeout, args.poll_seconds) if args.wait else 0
    except (ValueError, OSError, ImportError) as exc:
        # No request payload: it contains credentials.
        print(f"Configuration error ({type(exc).__name__}); check config, dependencies and credentials.", file=sys.stderr)
        if isinstance(exc, (ValueError, ImportError)) and not isinstance(exc, json.JSONDecodeError):
            print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Server error code/message help diagnose RAM, quota and network setup; never print the request.
        exc = getattr(exc, "inner_exception", None) or exc  # SDK wraps credential/network errors
        code, message = getattr(exc, "code", None), getattr(exc, "message", None)
        if type(exc).__name__ == "CredentialException":  # lists why each local credential source failed
            code = code or "LocalCredentials"
        # SDK messages can echo request parameters, including registry credentials.
        detail = f": {code}" if code else ""
        print(f"{stage} failed ({type(exc).__name__}{detail})", file=sys.stderr)
        if stage == "CreateJob":
            print("The job may still have been created; check the INSTANT console before resubmitting.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
