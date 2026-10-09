"""Deploy the support copilot to Amazon Bedrock AgentCore Runtime (direct code deploy, no Docker).

Idempotent: run it again after a code change and it updates the same runtime in place.

What it creates in your AWS account (all named db9-agentcore-demo* / db9/*, tagged project=db9-agentcore-demo):
  1. Secrets Manager  db9/agentcore-demo            default store's db9 DSN (from .env)
                      db9/tenants/<store_id>        one per store in .tenants.json (script 04 --keep)
  2. IAM role         db9-agentcore-demo-runtime    trusted by bedrock-agentcore; Bedrock invoke,
                                                    CloudWatch logs/X-Ray, and GetSecretValue on db9/* only
  3. S3 bucket        db9-agentcore-demo-<account>-<region>   the code zip
  4. AgentCore Runtime db9_agentcore_demo (PYTHON_3_12, PUBLIC network, HTTP protocol)
                      env: DB9_SECRET_ARN, DB9_TENANT_SECRET_PREFIX, BEDROCK_MODEL_ID

The runtime ARN is written to .agentcore.json (gitignored) for scripts/09_agentcore_e2e.py.
Tear everything down with deploy/destroy_agentcore.py.

Usage: python deploy/deploy_agentcore.py [--region us-west-2]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".agentcore.json"
NAME = "db9_agentcore_demo"           # runtime names: [a-zA-Z][a-zA-Z0-9_]{0,47}
ROLE = "db9-agentcore-demo-runtime"
DEFAULT_SECRET = "db9/agentcore-demo"
TENANT_PREFIX = "db9/tenants/"
TAGS = {"project": "db9-agentcore-demo"}
CODE = ["agentcore_app.py", "db9_agent"]   # never .env / .tenants.json: credentials live in Secrets Manager


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def put_secret(sm, name: str, value: str) -> str:
    try:
        arn = sm.create_secret(Name=name, SecretString=value,
                               Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["ARN"]
        log(f"secret created  {name}")
    except sm.exceptions.ResourceExistsException:
        arn = sm.put_secret_value(SecretId=name, SecretString=value)["ARN"]
        log(f"secret updated  {name}")
    return arn


def ensure_role(iam, account: str, region: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account},
                          "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}},
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Bedrock", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                          f"arn:aws:bedrock:*:{account}:inference-profile/*"]},
            {"Sid": "Db9Secrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": [f"arn:aws:secretsmanager:{region}:{account}:secret:{DEFAULT_SECRET}-*",
                          f"arn:aws:secretsmanager:{region}:{account}:secret:{TENANT_PREFIX}*"]},
            {"Sid": "Logs", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": [f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"]},
            {"Effect": "Allow", "Action": "logs:DescribeLogGroups",
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
            {"Effect": "Allow", "Resource": "*",
             "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                        "xray:GetSamplingRules", "xray:GetSamplingTargets"]},
            {"Effect": "Allow", "Resource": "*", "Action": "cloudwatch:PutMetricData",
             "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
            {"Sid": "WorkloadIdentity", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                        "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
             "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                          f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*"]},
        ],
    }
    try:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="AgentCore Runtime role for db9-agentcore-demo",
                              Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        log(f"role created    {ROLE}")
        fresh = True
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=ROLE, PolicyDocument=json.dumps(trust))
        log(f"role exists     {ROLE}")
        fresh = False
    iam.put_role_policy(RoleName=ROLE, PolicyName="runtime", PolicyDocument=json.dumps(policy))
    if fresh:
        time.sleep(15)  # IAM propagation before AgentCore validates the role
    return arn


def ensure_bucket(s3, account: str, region: str) -> str:
    bucket = f"db9-agentcore-demo-{account}-{region}"
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        kw = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=bucket, **kw)
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})
        log(f"bucket created  {bucket}")
    return bucket


def build_zip() -> Path:
    """AgentCore runs on Linux ARM64: install wheels for aarch64 / CPython 3.12.

    manylinux_2_28 (glibc 2.28), not manylinux2014: psycopg-binary 3.3 ships no 2014 aarch64
    wheel, and the AgentCore Python runtime's glibc is new enough for 2_28 wheels."""
    build = ROOT / "build"
    shutil.rmtree(build, ignore_errors=True)
    pkg = build / "package"
    pkg.mkdir(parents=True)
    subprocess.run(["uv", "pip", "install", "--quiet", "--target", str(pkg), "--python-version", "3.12",
                    "--python-platform", "aarch64-manylinux_2_28", "--only-binary", ":all:",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    for item in CODE:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, pkg / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, pkg / item)
    out = build / "agent.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(pkg))
    log(f"code zip        {out.stat().st_size / 1e6:.1f} MB")
    return out


def wait_ready(ctl, runtime_id: str, timeout: int = 900) -> dict:
    t0 = time.time()
    while True:
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            return rt
        if status.endswith("FAILED"):
            raise RuntimeError(f"runtime {status}: {rt.get('failureReason')}")
        if time.time() - t0 > timeout:
            raise TimeoutError(f"runtime still {status} after {timeout}s")
        time.sleep(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = ap.parse_args()
    region = args.region
    t0 = time.time()

    env = dotenv_values(ROOT / ".env")
    if not env.get("DB9_DATABASE_URL"):
        sys.exit("DB9_DATABASE_URL missing in .env (run the local setup first)")
    tenants = json.loads((ROOT / ".tenants.json").read_text()) if (ROOT / ".tenants.json").exists() else {}

    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    sm, iam, s3 = session.client("secretsmanager"), session.client("iam"), session.client("s3")
    ctl = session.client("bedrock-agentcore-control")

    default_arn = put_secret(sm, DEFAULT_SECRET, env["DB9_DATABASE_URL"])
    for store_id, entry in tenants.items():
        put_secret(sm, TENANT_PREFIX + store_id, entry["dsn"])
    role_arn = ensure_role(iam, account, region)
    bucket = ensure_bucket(s3, account, region)

    zip_path = build_zip()
    key = f"{NAME}/{int(time.time())}/agent.zip"   # new key per deploy -> new runtime version
    s3.upload_file(str(zip_path), bucket, key)
    log(f"uploaded        s3://{bucket}/{key}")

    spec = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": key}},
            "runtime": "PYTHON_3_12",
            "entryPoint": ["agentcore_app.py"],
        }},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},   # db9 is a public TLS endpoint, no PrivateLink
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables={
            "DB9_SECRET_ARN": default_arn,
            "DB9_TENANT_SECRET_PREFIX": TENANT_PREFIX,
            "BEDROCK_MODEL_ID": env.get("BEDROCK_MODEL_ID") or "us.anthropic.claude-sonnet-4-6",
        },
        description="db9 x AgentCore multi-store support copilot",
    )
    existing = [r for r in ctl.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"] == NAME]
    if existing:
        runtime_id = existing[0]["agentRuntimeId"]
        ctl.update_agent_runtime(agentRuntimeId=runtime_id, **spec)
        log(f"runtime update  {runtime_id}")
    else:
        rt = ctl.create_agent_runtime(agentRuntimeName=NAME, tags=TAGS, **spec)
        runtime_id = rt["agentRuntimeId"]
        log(f"runtime create  {runtime_id}")
    rt = wait_ready(ctl, runtime_id)

    state = {"region": region, "account": account, "runtime_id": runtime_id,
             "runtime_arn": rt["agentRuntimeArn"], "version": rt["agentRuntimeVersion"],
             "role": ROLE, "bucket": bucket, "secrets": [DEFAULT_SECRET] + [TENANT_PREFIX + s for s in tenants]}
    STATE.write_text(json.dumps(state, indent=2))
    log(f"READY           {rt['agentRuntimeArn']} (version {rt['agentRuntimeVersion']}) in {time.time() - t0:.0f}s")
    log(f"stores          nimbus-gear (default) + {', '.join(tenants) or 'none'}")


if __name__ == "__main__":
    main()
