"""db9 connection + schema bootstrap.

db9 speaks the PostgreSQL wire protocol, so the stock psycopg driver is all we
need. Everything "special" (embeddings, files, chunking) is plain SQL.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from functools import lru_cache
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
    for d in ("/kb", "/memories", "/runs", "/reports"):
        mkdirs(conn, d)
