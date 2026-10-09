"""Amazon Bedrock AgentCore Runtime entrypoint.

Local:   python agentcore_app.py        (serves POST /invocations on :8080)
Deploy:  python deploy/deploy_agentcore.py   (direct code deploy; test with scripts/09_agentcore_e2e.py)

Payload: {"prompt": "...", "user_id": "u-alice", "store_id": "peak-cycles", "session_id": "optional"}
         (store_id omitted -> the default store, nimbus-gear)
"""
from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

try:  # local runs; on AgentCore the env comes from the runtime config / Secrets Manager
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from db9_agent.agent import handle
from db9_agent.tenants import UnknownStore

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: RequestContext | None = None) -> dict:
    prompt = payload.get("prompt") or "Hello"
    user_id = payload.get("user_id", "anonymous")
    # Demo: the store comes from the payload. In production derive it from the caller's
    # verified identity (AgentCore inbound JWT auth, e.g. a `store_id` claim), never from
    # free-form input, so one store can't ask for another store's database.
    store_id = payload.get("store_id")
    # AgentCore gives every runtime session an id (X-Amzn-Bedrock-AgentCore-Runtime-Session-Id);
    # reuse it so the db9 conversation snapshot follows the AgentCore session.
    session_id = payload.get("session_id") or (getattr(context, "session_id", None) if context else None) or f"{user_id}-default"
    try:
        return handle(prompt, user_id, session_id, store_id)
    except UnknownStore as e:
        return {"status": "rejected", "error": str(e)}


if __name__ == "__main__":
    app.run()
