"""Step 9 - end-to-end integration test: the agent on AgentCore Runtime, working with db9.

Invokes the DEPLOYED runtime (deploy/deploy_agentcore.py -> .agentcore.json) through the
AgentCore data plane (bedrock-agentcore:InvokeAgentRuntime, SigV4), then checks db9 DIRECTLY
to prove what the agent did inside the managed microVM landed in the right store's database.

  T1 runtime        control plane says READY; the runtime has no db9 credential in code, only
                    DB9_SECRET_ARN / DB9_TENANT_SECRET_PREFIX env pointing at Secrets Manager
  T2 write path     session A (Nimbus Gear): the customer states durable facts
                    -> db9: agent_runs row keyed by the AgentCore session id, a memories row
                       + /memories/<user>/*.md file, /sessions/<sid>/messages.json, JSONL trace
  T3 short-term     same AgentCore session, follow-up turn -> history reloaded from fs9
  T4 long-term      NEW AgentCore session B (fresh microVM, empty chat history): the agent recalls
                    the membership from db9 and answers with the member-only 60-day window
  T5 tenant routing same user, store_id=peak-cycles -> answered from Peak Cycles' OWN database
                    (its policy, its run row); Nimbus memories do not leak; nothing written to Nimbus
  T6 fail closed    unknown / malformed store_id -> rejected, nothing written anywhere
  cleanup           delete every row and fs9 file the test created, stop the runtime sessions

Prereq: python scripts/04_saas_multi_store.py --keep (onboards peak-cycles) and
        python deploy/deploy_agentcore.py
Usage:  python scripts/09_agentcore_e2e.py [--keep-data]
"""
import argparse
import json
import pathlib
import re
import sys
import time
import uuid

import boto3
from dotenv import load_dotenv
from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from db9_agent.db import connect, database_url, literal  # noqa: E402
from db9_agent.tools import _slug  # noqa: E402

STATE_FILE = ROOT / ".agentcore.json"
TENANTS_FILE = ROOT / ".tenants.json"


class E2E:
    def __init__(self):
        if not STATE_FILE.exists():
            sys.exit("no .agentcore.json - run deploy/deploy_agentcore.py first")
        self.state = json.loads(STATE_FILE.read_text())
        tenants = json.loads(TENANTS_FILE.read_text()) if TENANTS_FILE.exists() else {}
        if "peak-cycles" not in tenants:
            sys.exit("peak-cycles not onboarded - run scripts/04_saas_multi_store.py --keep, then redeploy")
        self.dsn = {"nimbus-gear": database_url(), "peak-cycles": tenants["peak-cycles"]["dsn"]}
        region = self.state["region"]
        self.ctl = boto3.client("bedrock-agentcore-control", region_name=region)
        self.data = boto3.client("bedrock-agentcore", region_name=region)
        tag = uuid.uuid4().hex[:8]
        self.user = f"u-e2e-{tag}"
        # AgentCore runtime session ids must be >= 33 chars
        self.sid_a = f"e2e-{tag}-session-a-{uuid.uuid4().hex}"
        self.sid_b = f"e2e-{tag}-session-b-{uuid.uuid4().hex}"
        self.sid_peak = f"e2e-{tag}-session-peak-{uuid.uuid4().hex}"
        self.sid_bad = f"e2e-{tag}-session-bad-{uuid.uuid4().hex}"
        self.sessions = [self.sid_a, self.sid_b, self.sid_peak, self.sid_bad]
        self.results: list[tuple[str, bool, str]] = []

    # ---------- helpers ----------
    def invoke(self, sid: str, prompt: str, store_id: str | None) -> dict:
        payload = {"prompt": prompt, "user_id": self.user}
        if store_id is not None:
            payload["store_id"] = store_id
        t0 = time.time()
        r = self.data.invoke_agent_runtime(agentRuntimeArn=self.state["runtime_arn"], runtimeSessionId=sid,
                                           payload=json.dumps(payload).encode())
        body = json.loads(r["response"].read())
        body["_http"] = r["statusCode"]
        body["_wall_s"] = round(time.time() - t0, 1)
        print(f"    -> {store_id or 'default'} [{body['_wall_s']}s] {body.get('status')}: "
              f"{(body.get('answer') or body.get('error') or '')[:160]!r}")
        return body

    def q(self, store: str, sql: str, params=()) -> list[dict]:
        with connect(self.dsn[store]) as c:
            return c.execute(sql, params).fetchall()

    def trace_events(self, store: str, sid: str) -> list[dict]:
        # JSONL files are queried in place; each line arrives as a jsonb `line` column.
        # fs9() needs its glob as a literal, not a bind parameter.
        q = sql.SQL("SELECT line FROM extensions.fs9({}) ORDER BY line->>'ts'").format(
            literal(f"/runs/{_slug(sid, 64)}/*.jsonl"))
        return [r["line"] for r in self.q(store, q)]

    def tools_used(self, store: str, sid: str) -> list[str]:
        return [e["tool"] for e in self.trace_events(store, sid) if e.get("event") == "tool"]

    def exists(self, store: str, path: str) -> bool:
        return self.q(store, "SELECT extensions.fs9_exists(%s) AS e", (path,))[0]["e"]

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")

    # ---------- tests ----------
    def t1_runtime(self):
        print("T1 runtime")
        rt = self.ctl.get_agent_runtime(agentRuntimeId=self.state["runtime_id"])
        self.check("runtime READY", rt["status"] == "READY", f"version {rt['agentRuntimeVersion']}")
        env = rt.get("environmentVariables", {})
        self.check("db9 DSN comes from Secrets Manager, not the runtime config",
                   "DB9_SECRET_ARN" in env and not any("db9.io" in v for v in env.values()),
                   ", ".join(sorted(env)))

    def t2_write_path(self):
        print("T2 write path (session A, Nimbus Gear)")
        r = self.invoke(self.sid_a, "Hi! I'm a Nimbus Plus member and I always pick up orders at the Seattle "
                                    "locker. Please remember that for next time.", "nimbus-gear")
        self.check("invocation completed", r["_http"] == 200 and r.get("status") == "completed", r.get("run_id", ""))
        self.check("answered as Nimbus Gear", r.get("store") == "Nimbus Gear")
        runs = self.q("nimbus-gear", "SELECT run_id, status, latency_ms FROM agent_runs WHERE session_id=%s",
                      (self.sid_a,))
        self.check("agent_runs row keyed by the AgentCore session id",
                   len(runs) == 1 and runs[0]["run_id"] == r.get("run_id") and runs[0]["status"] == "completed",
                   f"{len(runs)} row(s)")
        mems = self.q("nimbus-gear", "SELECT topic, file_path, summary, embedding IS NOT NULL AS has_vec "
                                     "FROM memories WHERE user_id=%s", (self.user,))
        self.check("memory row written with a vector", mems and all(m["has_vec"] for m in mems),
                   "; ".join(m["summary"][:60] for m in mems))
        self.check("memory note file in fs9", mems and all(self.exists("nimbus-gear", m["file_path"]) for m in mems),
                   ", ".join(m["file_path"] for m in mems))
        self.check("conversation snapshot in fs9",
                   self.exists("nimbus-gear", f"/sessions/{_slug(self.sid_a, 64)}/messages.json"))
        events = self.trace_events("nimbus-gear", self.sid_a)
        kinds = [e["event"] for e in events]
        self.check("JSONL trace has start / tool / end", kinds[:1] == ["start"] and kinds[-1:] == ["end"]
                   and "remember" in self.tools_used("nimbus-gear", self.sid_a),
                   " > ".join(e.get("tool", e["event"]) for e in events))

    def t3_short_term(self):
        print("T3 short-term memory (same AgentCore session)")
        n_before = len(json.loads(self.q("nimbus-gear", "SELECT extensions.fs9_read(%s) AS t",
                                         (f"/sessions/{_slug(self.sid_a, 64)}/messages.json",))[0]["t"]))
        r = self.invoke(self.sid_a, "Which pickup location did I just mention? One short sentence.", "nimbus-gear")
        n_after = len(json.loads(self.q("nimbus-gear", "SELECT extensions.fs9_read(%s) AS t",
                                        (f"/sessions/{_slug(self.sid_a, 64)}/messages.json",))[0]["t"]))
        self.check("answer uses the earlier turn", r.get("status") == "completed"
                   and "seattle" in r.get("answer", "").lower())
        self.check("session snapshot grew", n_after > n_before, f"{n_before} -> {n_after} messages")

    def t4_long_term(self):
        print("T4 long-term memory (NEW AgentCore session, empty history)")
        r = self.invoke(self.sid_b, "I used my tent on one camping trip. How many days do I have to return it? "
                                    "Give the number of days.", "nimbus-gear")
        snap = f"/sessions/{_slug(self.sid_b, 64)}/messages.json"
        msgs = json.loads(self.q("nimbus-gear", "SELECT extensions.fs9_read(%s) AS t", (snap,))[0]["t"])
        first = " ".join(part.get("text", "") for part in msgs[0]["content"]) if msgs else ""
        self.check("session B started with empty history", r.get("status") == "completed"
                   and "camping trip" in first, "first stored message is this turn's prompt")
        tools = self.tools_used("nimbus-gear", self.sid_b)
        self.check("agent called recall + search_knowledge", "recall" in tools and "search_knowledge" in tools,
                   ", ".join(tools))
        self.check("member-only 60-day window from recalled membership", re.search(r"\b60\b", r.get("answer", "")),
                   "membership was only stated in session A")

    def t5_tenant_routing(self):
        print("T5 tenant routing (same user, Peak Cycles)")
        r = self.invoke(self.sid_peak, "I rode my new bike once. Can I still return it? Answer briefly.", "peak-cycles")
        self.check("answered as Peak Cycles", r.get("status") == "completed" and r.get("store") == "Peak Cycles")
        peak_runs = self.q("peak-cycles", "SELECT count(*) AS n FROM agent_runs WHERE session_id=%s", (self.sid_peak,))
        nimbus_runs = self.q("nimbus-gear", "SELECT count(*) AS n FROM agent_runs WHERE session_id=%s", (self.sid_peak,))
        self.check("run recorded in Peak Cycles' database only",
                   peak_runs[0]["n"] == 1 and nimbus_runs[0]["n"] == 0,
                   f"peak={peak_runs[0]['n']} nimbus={nimbus_runs[0]['n']}")
        hits = [e for e in self.trace_events("peak-cycles", self.sid_peak)
                if e.get("event") == "tool" and e.get("tool") == "search_knowledge"]
        self.check("grounded in Peak Cycles' own KB", hits and self.q(
            "peak-cycles", "SELECT count(*) AS n FROM kb_chunks WHERE chunk_text ILIKE '%%ridden%%'")[0]["n"] > 0)
        ans = r.get("answer", "").lower()
        self.check("Peak policy: ridden bikes are not returnable",
                   re.search(r"not (be )?returnable|non-returnable|can(no|')t (be )?return|aren't returnable|"
                             r"no longer (be )?returnable|unfortunately|isn't returnable|not eligible", ans),
                   "Nimbus would have said 30/60 days")
        leaked = self.q("peak-cycles", "SELECT count(*) AS n FROM memories WHERE user_id=%s", (self.user,))
        self.check("Nimbus memories not visible in Peak Cycles", leaked[0]["n"] == 0 and "nimbus plus" not in ans)

    def t6_fail_closed(self):
        print("T6 fail closed")
        for store in ("tidepool-surf", "../nimbus-gear"):
            r = self.invoke(self.sid_bad, "What is your return policy?", store)
            self.check(f"store_id {store!r} rejected", r.get("status") == "rejected" and "answer" not in r,
                       r.get("error", ""))
        written = sum(self.q(s, "SELECT count(*) AS n FROM agent_runs WHERE session_id=%s", (self.sid_bad,))[0]["n"]
                      for s in self.dsn)
        self.check("nothing written for rejected requests", written == 0)

    # ---------- cleanup ----------
    def cleanup(self):
        for store in self.dsn:
            with connect(self.dsn[store]) as c:
                for row in c.execute("SELECT file_path FROM memories WHERE user_id=%s", (self.user,)).fetchall():
                    c.execute("SELECT extensions.fs9_remove(%s, true)", (row["file_path"],))
                c.execute("DELETE FROM memories WHERE user_id=%s", (self.user,))
                c.execute("DELETE FROM agent_runs WHERE user_id=%s", (self.user,))
                for p in [f"/memories/{_slug(self.user)}"] + [f"/{d}/{_slug(s, 64)}" for s in self.sessions
                                                               for d in ("sessions", "runs", "reports")]:
                    if c.execute("SELECT extensions.fs9_exists(%s) AS e", (p,)).fetchone()["e"]:
                        c.execute("SELECT extensions.fs9_remove(%s, true)", (p,))
        for sid in self.sessions:
            try:
                self.data.stop_runtime_session(agentRuntimeArn=self.state["runtime_arn"], runtimeSessionId=sid)
            except Exception:
                pass  # already idle / never started
        print(f"cleanup: removed test data for {self.user}, stopped {len(self.sessions)} runtime sessions")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-data", action="store_true", help="leave the test rows/files in db9 for inspection")
    args = ap.parse_args()
    t = E2E()
    print(f"runtime {t.state['runtime_arn']}\nuser    {t.user}\n")
    t0 = time.time()
    try:
        for step in (t.t1_runtime, t.t2_write_path, t.t3_short_term, t.t4_long_term, t.t5_tenant_routing,
                     t.t6_fail_closed):
            try:
                step()
            except Exception as e:  # one broken step should not hide the others
                t.check(f"{step.__name__} raised", False, f"{type(e).__name__}: {e}")
    finally:
        if not args.keep_data:
            t.cleanup()
    passed = sum(ok for _, ok, _ in t.results)
    print(f"\n{passed}/{len(t.results)} checks passed in {time.time() - t0:.0f}s")
    sys.exit(0 if passed == len(t.results) else 1)


if __name__ == "__main__":
    main()
