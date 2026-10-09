"""Amazon Bedrock AgentCore Runtime entrypoint.

Local:   python agentcore_app.py        (serves POST /invocations on :8080)
Deploy:  agentcore configure -e agentcore_app.py && agentcore launch   (see README)

Payload: {"prompt": "...", "user_id": "u-alice", "session_id": "optional"}
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

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: RequestContext | None = None) -> dict:
    prompt = payload.get("prompt") or "Hello"
    user_id = payload.get("user_id", "anonymous")
    # AgentCore gives every runtime session an id (X-Amzn-Bedrock-AgentCore-Runtime-Session-Id);
    # reuse it so the db9 conversation snapshot follows the AgentCore session.
    session_id = payload.get("session_id") or (getattr(context, "session_id", None) if context else None) or f"{user_id}-default"
    return handle(prompt, user_id, session_id)


if __name__ == "__main__":
    app.run()
