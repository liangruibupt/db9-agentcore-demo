"""The support-copilot agent: Strands + Amazon Bedrock (Claude) + db9.

Multi-store SaaS: ONE agent implementation serves many stores. Every request is
routed to the store's own db9 database (tenants.resolve); the store's brand
config, KB, orders and customer memories all come from that database only.
"""
from __future__ import annotations

import json
import os
import time

from strands import Agent
from strands.models import BedrockModel

from .db import connect, mkdirs, store_config
from .tenants import resolve
from .tools import Tracer, _slug, build_tools, new_run_id

SYSTEM_PROMPT = """You are the customer-support copilot for "{name}", {blurb}. Tone: {tone}.
You only know this store. Tools you have:
- recall: ALWAYS call first to load what we know about this customer.
- search_knowledge: ground every policy answer in the knowledge base; cite the source file path.
- query_data: read-only SQL over the orders table for order questions (always filter by the current user_id).
- remember: store NEW durable facts/preferences the customer reveals (not one-off chit-chat).
- save_report: when asked for a summary/case report, save it.
Answer in the customer's language. Be concise. Never invent policy details."""


def _session_file(session_id: str) -> str:
    return f"/sessions/{_slug(session_id, 64)}/messages.json"


def _load_messages(dsn: str, session_id: str) -> list:
    """Short-term memory: the conversation snapshot is just a JSON file in fs9."""
    path = _session_file(session_id)
    with connect(dsn) as c:
        if c.execute("SELECT extensions.fs9_exists(%s) AS e", (path,)).fetchone()["e"]:
            return json.loads(c.execute("SELECT extensions.fs9_read(%s) AS t", (path,)).fetchone()["t"])
    return []


def _save_messages(dsn: str, session_id: str, messages: list) -> None:
    path = _session_file(session_id)
    with connect(dsn) as c:
        mkdirs(c, path.rsplit("/", 1)[0])
        c.execute("SELECT extensions.fs9_write(%s, %s)", (path, json.dumps(messages, ensure_ascii=False, default=str)))


def handle(prompt: str, user_id: str, session_id: str, store_id: str | None = None) -> dict:
    dsn = resolve(store_id)  # fails closed for unknown stores
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    run_id = new_run_id()
    tracer = Tracer(session_id, run_id, dsn)
    tracer.log("start", user_id=user_id, prompt=prompt, model=model_id, store=store_id)
    with connect(dsn) as c:
        cfg = store_config(c)
        c.execute(
            "INSERT INTO agent_runs (run_id, session_id, user_id, prompt, status, model_id, trace_path) "
            "VALUES (%s,%s,%s,%s,'running',%s,%s)",
            (run_id, session_id, user_id, prompt, model_id, tracer.path),
        )

    agent = Agent(
        model=BedrockModel(model_id=model_id, region_name=os.environ.get("AWS_REGION", "us-west-2")),
        system_prompt=SYSTEM_PROMPT.format(name=cfg["name"], blurb=cfg["blurb"], tone=cfg.get("tone", "helpful"))
        + f"\nCurrent user_id: {user_id}",
        tools=build_tools(user_id, tracer, dsn),
        messages=_load_messages(dsn, session_id),
        callback_handler=None,
    )
    t0 = time.time()
    status = "completed"
    try:
        answer = str(agent(prompt))
    except Exception as e:
        status, answer = "failed", f"ERROR: {e}"
    latency = int((time.time() - t0) * 1000)

    _save_messages(dsn, session_id, agent.messages)
    tracer.log("end", status=status, ms=latency, answer=answer[:2000])
    with connect(dsn) as c:
        c.execute("UPDATE agent_runs SET status=%s, latency_ms=%s WHERE run_id=%s", (status, latency, run_id))
    return {"store": cfg["name"], "run_id": run_id, "status": status, "latency_ms": latency,
            "trace": tracer.path, "answer": answer}
