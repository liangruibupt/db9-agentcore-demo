"""Step 4 - database-per-agent / per-tenant: provision real Postgres in about a second.

Typical use: each AgentCore session (or each customer, or each CI run) gets its own
isolated database + file system, used for scratch state and deleted afterwards.
Compare: a new Aurora Serverless v2 cluster takes minutes; db9 is one API call.
"""
import json
import os
import shutil
import subprocess
import time

import psycopg

DB9 = shutil.which("db9") or os.path.expanduser("~/.local/bin/db9")
N = int(os.environ.get("TENANTS", "2"))  # anonymous accounts are capped at 5 databases total


def db9(*args):
    return json.loads(subprocess.run([DB9, *args, "--json"], check=True, capture_output=True, text=True).stdout)


def main():
    names = [f"tenant-{int(time.time()) % 10000}-{i}" for i in range(N)]
    try:
        for name in names:
            t0 = time.time()
            db9("create", "--name", name)
            t_create = time.time() - t0
            url = db9("db", "connect", name)["connection_string"]
            t1 = time.time()
            with psycopg.connect(url, autocommit=True) as c:
                c.execute("CREATE TABLE scratch (k TEXT PRIMARY KEY, v JSONB)")
                c.execute("INSERT INTO scratch VALUES ('plan', %s)", (json.dumps({"step": 1, "tenant": name}),))
                c.execute("SELECT extensions.fs9_write('/hello.txt', %s)", (f"hi from {name}",))
                v = c.execute("SELECT v->>'tenant' FROM scratch").fetchone()[0]
            print(f"{name}: create={t_create:.2f}s  first query+fs write={time.time() - t1:.2f}s  -> {v}")
    finally:
        for name in names:
            subprocess.run([DB9, "delete", name, "--yes"], capture_output=True)
        print(f"deleted {len(names)} tenant databases")


if __name__ == "__main__":
    main()
