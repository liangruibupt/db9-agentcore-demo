"""db9 connection + schema bootstrap.

db9 speaks the PostgreSQL wire protocol, so the stock psycopg driver is all we
need. Everything "special" (embeddings, files, chunking) is plain SQL.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


@lru_cache(maxsize=1)
def database_url() -> str:
    """Resolve the db9 DSN.

    Local runs read DB9_DATABASE_URL from .env. On AgentCore Runtime set
    DB9_SECRET_ARN instead and keep the DSN in AWS Secrets Manager, so the
    password never lands in the container image or runtime env config.
    """
    arn = os.environ.get("DB9_SECRET_ARN")
    if arn:
        import boto3

        sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))
        value = sm.get_secret_value(SecretId=arn)["SecretString"]
        try:  # allow either a raw DSN or {"DB9_DATABASE_URL": "..."}
            return json.loads(value)["DB9_DATABASE_URL"]
        except (ValueError, KeyError, TypeError):
            return value
    url = os.environ.get("DB9_DATABASE_URL")
    if not url:
        raise RuntimeError("Set DB9_DATABASE_URL (local) or DB9_SECRET_ARN (AgentCore)")
    return url


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(url or database_url(), autocommit=True, row_factory=dict_row) as conn:
        yield conn


def literal(value: str) -> sql.Composable:
    """Safely inline a text literal.

    db9's HNSW index is only used when the vector probe is an inline literal
    (VEC_EMBED_COSINE_DISTANCE(col, 'text')), not a bound $1 parameter, so we
    inline with proper quoting instead of string formatting.
    """
    return sql.Literal(value)


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS embedding;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS fs9;

-- Knowledge base: source files live in fs9 (/kb/*.md), chunks + vectors in SQL.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id          SERIAL PRIMARY KEY,
    file_path   TEXT NOT NULL,
    chunk_index INT  NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(1024)
);

-- Long-term memory: structured row + the raw note as a file.
CREATE TABLE IF NOT EXISTS memories (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    topic      TEXT NOT NULL,
    file_path  TEXT NOT NULL,
    summary    TEXT NOT NULL,
    embedding  VECTOR(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Business data the agent can analyse with read-only SQL.
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    product    TEXT NOT NULL,
    amount_usd NUMERIC(10,2) NOT NULL,
    status     TEXT NOT NULL,
    ordered_at DATE NOT NULL
);

-- Agent run history (metadata). Per-step traces go to fs9 as JSONL.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL,
    model_id    TEXT,
    latency_ms  INT,
    trace_path  TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS kb_chunks_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS memories_hnsw  ON memories  USING hnsw (embedding vector_cosine_ops);
"""


def mkdirs(conn: psycopg.Connection, path: str) -> None:
    if not conn.execute("SELECT extensions.fs9_exists(%s) AS e", (path,)).fetchone()["e"]:
        conn.execute("SELECT extensions.fs9_mkdir(%s, true)", (path,))


def bootstrap(conn: psycopg.Connection) -> None:
    for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
        conn.execute(stmt)
    for stmt in filter(None, (s.strip() for s in INDEXES.split(";"))):
        conn.execute(stmt)
    for d in ("/config", "/kb", "/memories", "/runs", "/reports", "/sessions"):
        mkdirs(conn, d)


# The whole RAG ingest is ONE statement inside the database:
# list fs9 directory -> read file -> chunk -> embed -> insert. No S3, no Lambda, no pipeline.
INGEST_SQL = """
INSERT INTO kb_chunks (file_path, chunk_index, chunk_text, embedding)
SELECT f.path, c.chunk_index, c.chunk_text, embedding(c.chunk_text)
FROM extensions.fs9('/kb/') AS f
CROSS JOIN LATERAL CHUNK_TEXT(
    content       => extensions.fs9_read(f.path),
    max_chars     => 600,
    overlap_chars => 80,
    title         => f.path
) AS c
WHERE f.type = 'file' AND f.path LIKE '%.md'
"""


def provision_store(conn: psycopg.Connection, store_dir: Path) -> dict:
    """Turn an empty db9 database into one store's complete agent backend.

    store_dir/store.json -> /config/store.json (brand config the agent reads at runtime)
                            + orders rows
    store_dir/kb/*.md    -> /kb/*.md in fs9 -> chunked + embedded in SQL
    Idempotent: re-running replaces the KB and keeps existing orders.
    """
    cfg = json.loads((store_dir / "store.json").read_text())
    bootstrap(conn)
    conn.execute("SELECT extensions.fs9_write('/config/store.json', %s)",
                 (json.dumps({k: v for k, v in cfg.items() if k != "orders"}, ensure_ascii=False),))
    for md in sorted((store_dir / "kb").glob("*.md")):
        conn.execute("SELECT extensions.fs9_write(%s, %s)", (f"/kb/{md.name}", md.read_text()))
    conn.execute("DELETE FROM kb_chunks")
    conn.execute(INGEST_SQL)
    conn.cursor().executemany(
        "INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING", cfg["orders"])
    stats = conn.execute("SELECT (SELECT count(DISTINCT file_path) FROM kb_chunks) AS files, "
                         "(SELECT count(*) FROM kb_chunks) AS chunks, "
                         "(SELECT count(*) FROM orders) AS orders").fetchone()
    return {"store_id": cfg["store_id"], "name": cfg["name"], **stats}


def store_config(conn: psycopg.Connection) -> dict:
    """The store's brand config lives in its own database (fs9), not in the agent code."""
    if conn.execute("SELECT extensions.fs9_exists('/config/store.json') AS e").fetchone()["e"]:
        return json.loads(conn.execute("SELECT extensions.fs9_read('/config/store.json') AS t").fetchone()["t"])
    return {"name": "Nimbus Gear", "blurb": "an online outdoor-gear store", "tone": "friendly and practical"}
