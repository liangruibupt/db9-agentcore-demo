"""Strands tools backed by db9.

Each tool maps to one db9 capability:
  search_knowledge  -> built-in embedding() + HNSW vector index
  remember / recall -> "memory in tables, context in files" (SQL row + fs9 file)
  query_data        -> plain Postgres SQL (read-only transaction)
  save_report       -> fs9 file output, discoverable via SQL
Every tool call is also appended to a JSONL trace file in fs9 (see Tracer).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

from psycopg import sql
from strands import tool

from .db import connect, literal, mkdirs

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slug(s: str, n: int = 40) -> str:
    return _SAFE.sub("-", s.strip().lower())[:n].strip("-") or "note"


class Tracer:
    """Append one JSON line per event to /runs/<session>/<run>.jsonl in fs9."""

    def __init__(self, session_id: str, run_id: str):
        self.path = f"/runs/{_slug(session_id, 64)}/{run_id}.jsonl"
        with connect() as c:
            mkdirs(c, self.path.rsplit("/", 1)[0])

    def log(self, event: str, **data) -> None:
        line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data},
                          ensure_ascii=False, default=str)
        with connect() as c:
            # Leading/trailing newline: fs9_append writes bytes verbatim.
            c.execute("SELECT extensions.fs9_append(%s, %s)", (self.path, "\n" + line + "\n"))


def build_tools(user_id: str, tracer: Tracer):
    """Create tools bound to one user + run (closures keep tenant scoping out of the LLM's hands)."""

    def traced(name: str, args: dict, fn):
        t0 = time.time()
        try:
            out = fn()
            tracer.log("tool", tool=name, args=args, ms=int((time.time() - t0) * 1000), ok=True)
            return out
        except Exception as e:  # surface DB errors to the model instead of crashing the run
            tracer.log("tool", tool=name, args=args, ms=int((time.time() - t0) * 1000), ok=False, error=str(e))
            return f"ERROR: {e}"

    @tool
    def search_knowledge(query: str, k: int = 4) -> str:
        """Semantic search over the company knowledge base (policies, FAQs, product docs).

        Args:
            query: natural-language question
            k: number of chunks to return (1-8)
        """
        def run():
            # Inline literal probe -> db9 uses the HNSW index; embedding happens server-side.
            q = sql.SQL(
                "SELECT file_path, chunk_index, chunk_text, "
                "VEC_EMBED_COSINE_DISTANCE(embedding, {q}) AS dist "
                "FROM kb_chunks ORDER BY VEC_EMBED_COSINE_DISTANCE(embedding, {q}) LIMIT {k}"
            ).format(q=literal(query), k=sql.Literal(max(1, min(int(k), 8))))
            with connect() as c:
                rows = c.execute(q).fetchall()
            return json.dumps([{"source": r["file_path"], "chunk": r["chunk_index"],
                                "distance": round(float(r["dist"]), 4), "text": r["chunk_text"]}
                               for r in rows], ensure_ascii=False)
        return traced("search_knowledge", {"query": query, "k": k}, run)

    @tool
    def remember(topic: str, note: str) -> str:
        """Save a durable fact or preference about the current user for future conversations.

        Args:
            topic: short topic label, e.g. "shipping-preference"
            note: the fact to remember, in one or two sentences
        """
        def run():
            path = f"/memories/{_slug(user_id)}/{int(time.time())}-{_slug(topic)}.md"
            body = f"---\nuser: {user_id}\ntopic: {topic}\ncreated: {datetime.now(timezone.utc).isoformat()}\n---\n{note}\n"
            with connect() as c:
                mkdirs(c, path.rsplit("/", 1)[0])
                c.execute("SELECT extensions.fs9_write(%s, %s)", (path, body))           # context -> file
                c.execute(                                                               # state -> table
                    "INSERT INTO memories (user_id, topic, file_path, summary, embedding) "
                    "VALUES (%s, %s, %s, %s, embedding(%s))",
                    (user_id, topic, path, note, f"{topic}: {note}"),
                )
            return f"saved to {path}"
        return traced("remember", {"topic": topic}, run)

    @tool
    def recall(query: str, k: int = 3) -> str:
        """Recall what we already know about the current user (preferences, past issues).

        Args:
            query: what you want to recall
            k: max memories to return
        """
        def run():
            with connect() as c:
                rows = c.execute(
                    "SELECT topic, summary, file_path, created_at, "
                    "embedding <=> embedding(%s) AS dist FROM memories "
                    "WHERE user_id = %s ORDER BY dist LIMIT %s",
                    (query, user_id, max(1, min(int(k), 10))),
                ).fetchall()
            return json.dumps(rows, ensure_ascii=False, default=str)
        return traced("recall", {"query": query}, run)

    @tool
    def query_data(sql_query: str) -> str:
        """Run a read-only SQL query (PostgreSQL dialect) against business tables.

        Tables: orders(order_id, user_id, product, amount_usd, status, ordered_at),
        agent_runs(run_id, session_id, user_id, prompt, status, model_id, latency_ms, trace_path, started_at).
        Only SELECT/WITH statements are allowed; results are capped at 50 rows.

        Args:
            sql_query: a single SELECT statement
        """
        def run():
            s = sql_query.strip().rstrip(";")
            if not re.match(r"(?is)^\s*(select|with)\b", s) or ";" in s:
                return "ERROR: only a single SELECT/WITH statement is allowed"
            with connect() as c:
                with c.transaction():
                    c.execute("SET TRANSACTION READ ONLY")
                    rows = c.execute(s).fetchmany(50)
            return json.dumps(rows, ensure_ascii=False, default=str)
        return traced("query_data", {"sql": sql_query}, run)

    @tool
    def save_report(title: str, markdown: str) -> str:
        """Persist a markdown report/artifact (e.g. a case summary) to the shared file system.

        Args:
            title: report title
            markdown: full report body in markdown
        """
        def run():
            path = f"/reports/{_slug(user_id)}/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{_slug(title)}.md"
            with connect() as c:
                mkdirs(c, path.rsplit("/", 1)[0])
                body = markdown if markdown.lstrip().startswith("#") else f"# {title}\n\n{markdown}"
                c.execute("SELECT extensions.fs9_write(%s, %s)", (path, body.rstrip() + "\n"))
            return f"report saved to {path}"
        return traced("save_report", {"title": title}, run)

    return [search_knowledge, remember, recall, query_data, save_report]


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
