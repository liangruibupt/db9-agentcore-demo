"""Tear down everything deploy/deploy_agentcore.py created (reads .agentcore.json).

Deletes: the AgentCore runtime, the S3 code bucket (all objects), the IAM role, and the
db9/* secrets (with Secrets Manager's default 7-day recovery window, so a mistake is undoable).
Does NOT delete any db9 database: the stores stay usable locally.

Usage: python deploy/destroy_agentcore.py --yes

To redeploy within those 7 days, first restore the secrets
(aws secretsmanager restore-secret --secret-id db9/agentcore-demo), because a name pending
deletion cannot be re-created.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".agentcore.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="confirm deletion")
    args = ap.parse_args()
    if not STATE.exists():
        sys.exit("no .agentcore.json - nothing deployed from this checkout")
    st = json.loads(STATE.read_text())
    print(json.dumps(st, indent=2))
    if not args.yes:
        sys.exit("re-run with --yes to delete the resources above")

    session = boto3.Session(region_name=st["region"])
    ctl = session.client("bedrock-agentcore-control")
    ctl.delete_agent_runtime(agentRuntimeId=st["runtime_id"])
    while any(r["agentRuntimeId"] == st["runtime_id"] for r in ctl.list_agent_runtimes()["agentRuntimes"]):
        time.sleep(5)
    print(f"deleted runtime {st['runtime_id']}")

    bucket = session.resource("s3").Bucket(st["bucket"])
    bucket.objects.all().delete()
    bucket.delete()
    print(f"deleted bucket  {st['bucket']}")

    iam = session.client("iam")
    for name in iam.list_role_policies(RoleName=st["role"])["PolicyNames"]:
        iam.delete_role_policy(RoleName=st["role"], PolicyName=name)
    iam.delete_role(RoleName=st["role"])
    print(f"deleted role    {st['role']}")

    sm = session.client("secretsmanager")
    for name in st["secrets"]:
        sm.delete_secret(SecretId=name, RecoveryWindowInDays=7)
        print(f"scheduled secret deletion (7 days) {name}")
    STATE.unlink()


if __name__ == "__main__":
    main()
