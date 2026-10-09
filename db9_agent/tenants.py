"""Multi-store SaaS control plane: one db9 database per store (tenant).

Each store that signs up for the support copilot gets its OWN db9 database holding
its knowledge base, orders, customer memories, conversation snapshots, traces and
reports. The agent code is shared; the data never is.

  store_id -> DSN resolution
    default store (nimbus-gear) : DB9_DATABASE_URL / DB9_SECRET_ARN (see db.database_url)
    AgentCore Runtime           : Secrets Manager secret  <DB9_TENANT_SECRET_PREFIX><store_id>
                                  e.g. db9/tenants/peak-cycles  (raw DSN string)
    local demo                  : .tenants.json (gitignored, chmod 600)

onboard()/offboard() drive the db9 CLI; a real control plane would call the db9
REST API from its signup / cancellation workflow instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .db import connect, database_url, provision_store

ROOT = Path(__file__).resolve().parents[1]
STORES_DIR = ROOT / "stores"
REGISTRY = ROOT / ".tenants.json"
DEFAULT_STORE = "nimbus-gear"
DB9 = shutil.which("db9") or os.path.expanduser("~/.local/bin/db9")
_STORE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class UnknownStore(LookupError):
    pass


def _db9(*args: str) -> dict | list:
    out = subprocess.run([DB9, *args, "--json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def _load() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _save(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2))
    REGISTRY.chmod(0o600)  # holds admin DSNs


def _check(store_id: str) -> str:
    if not _STORE_ID.match(store_id or ""):
        raise UnknownStore(f"invalid store_id {store_id!r}")
    return store_id


@lru_cache(maxsize=256)
def _secret_dsn(secret_id: str) -> str:
    import boto3

    sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))
    return sm.get_secret_value(SecretId=secret_id)["SecretString"]


def resolve(store_id: str | None) -> str:
    """Map a store to its own database. Unknown stores fail closed (never fall back to another store's DB)."""
    if not store_id or store_id == DEFAULT_STORE:
        return database_url()
    _check(store_id)
    prefix = os.environ.get("DB9_TENANT_SECRET_PREFIX")
    if prefix:
        return _secret_dsn(prefix + store_id)
    entry = _load().get(store_id)
    if not entry:
        raise UnknownStore(f"store {store_id!r} is not onboarded")
    return entry["dsn"]


def _with_password(conn_str: str, password: str) -> str:
    u = urlsplit(conn_str)
    user, host = u.netloc.split("@", 1)
    user = user.split(":", 1)[0]
    query = u.query or "sslmode=require"
    return urlunsplit((u.scheme, f"{user}:{quote(password, safe='')}@{host}", u.path, query, ""))


def onboard(store_id: str) -> dict:
    """Store signs up: create its database and provision KB + orders + config. ~ seconds, not minutes."""
    _check(store_id)
    store_dir = STORES_DIR / store_id
    if not (store_dir / "store.json").exists():
        raise UnknownStore(f"no seed data in {store_dir}")
    reg = _load()
    if store_id in reg:
        raise ValueError(f"store {store_id!r} already onboarded ({reg[store_id]['db_name']})")

    t0 = time.time()
    created = _db9("create", "--name", f"store-{store_id}", "--show-password")
    password = next(v for k, v in created.items() if "password" in k and isinstance(v, str) and v)
    dsn = _with_password(created["connection_string"], password)
    t_create = time.time() - t0
    reg[store_id] = {"db_name": created["name"], "db_id": created["id"], "dsn": dsn}
    _save(reg)

    t1 = time.time()
    with connect(dsn) as c:
        stats = provision_store(c, store_dir)
    return {**stats, "db_name": created["name"], "create_s": round(t_create, 1),
            "provision_s": round(time.time() - t1, 1)}


def offboard(store_id: str) -> str:
    """Store cancels: one call deletes ALL of its data (tables, vectors, files, memories, traces)."""
    reg = _load()
    entry = reg.pop(_check(store_id), None)
    if not entry:
        raise UnknownStore(f"store {store_id!r} is not onboarded")
    subprocess.run([DB9, "delete", entry["db_name"], "--yes"], check=True, capture_output=True)
    _save(reg)
    return entry["db_name"]


def onboarded() -> list[str]:
    return [DEFAULT_STORE, *sorted(_load())]
