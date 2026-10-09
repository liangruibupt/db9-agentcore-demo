"""db9 database lifecycle helpers (create / branch / delete) used by the platform demos.

These drive the db9 CLI; a production control plane would call the db9 REST API.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from urllib.parse import quote, urlsplit, urlunsplit

DB9 = shutil.which("db9") or os.path.expanduser("~/.local/bin/db9")


def db9(*args: str) -> dict | list:
    out = subprocess.run([DB9, *args, "--json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def _with_password(conn_str: str, password: str) -> str:
    u = urlsplit(conn_str)
    user, host = u.netloc.split("@", 1)
    user = user.split(":", 1)[0]
    return urlunsplit((u.scheme, f"{user}:{quote(password, safe='')}@{host}", u.path, u.query or "sslmode=require", ""))


def create_database(name: str) -> dict:
    """Create a database and return {name, id, dsn, seconds}. The DSN carries the admin password."""
    t0 = time.time()
    created = db9("create", "--name", name, "--show-password")
    password = next(v for k, v in created.items() if "password" in k and isinstance(v, str) and v)
    return {"name": created["name"], "id": created["id"],
            "dsn": _with_password(created["connection_string"], password), "seconds": round(time.time() - t0, 1)}


def branch_database(source: str, name: str, timeout_s: int = 300) -> dict:
    """Branch a database (full copy incl. fs9 files) and wait until ACTIVE.

    Returns a short-lived (<=15 min) DSN minted by `db9 db connect`.
    """
    t0 = time.time()
    db9("branch", "create", source, "--name", name)
    while True:
        state = next((d["state"] for d in db9("list") if d["name"] == name), "MISSING")
        if state == "ACTIVE":
            break
        if time.time() - t0 > timeout_s:
            raise TimeoutError(f"branch {name} still {state} after {timeout_s}s")
        time.sleep(5)
    return {"name": name, "dsn": db9("db", "connect", name)["connection_string"], "seconds": round(time.time() - t0)}


def delete_database(name: str) -> None:
    subprocess.run([DB9, "delete", name, "--yes"], check=True, capture_output=True)
