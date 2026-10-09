"""Step 2 - multi-turn agent demo (Bedrock Claude + db9), then inspect what the agent left behind.

Session 1 (alice): asks about a tent return, reveals a preference -> agent remembers it.
Session 2 (alice, NEW session, empty chat history): agent recalls the preference from db9.
Finally: query the agent's own traces (JSONL files in fs9) with SQL.
"""
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from db9_agent.agent import handle  # noqa: E402
from db9_agent.db import connect  # noqa: E402

TURNS = [
    ("s1", "I bought the Nimbus Trail 2P tent last month and used it on one trip. Can I still return it? "
           "By the way I'm a Nimbus Plus member and I always prefer locker pickup in Seattle."),
    ("s1", "OK. Also, what's the status of my other recent orders?"),
    ("s2", "Hi again! I want to order a sleeping bag - how will it be delivered to me, and what's my return window?"),
    ("s2", "Please save a short case summary report of my situation."),
]


def main():
    tag = uuid.uuid4().hex[:4]
    sessions = {}
    for key, prompt in TURNS:
        sid = sessions.setdefault(key, f"alice-{key}-{tag}")
        print(f"\n\033[1m[{sid}] alice:\033[0m {prompt}")
        out = handle(prompt, "u-alice", sid)
        print(f"\033[1magent ({out['latency_ms']} ms, {out['run_id']}):\033[0m {out['answer']}")

    with connect() as c:
        print("\n=== memories table (state) ===")
        for r in c.execute("SELECT topic, summary, file_path FROM memories WHERE user_id='u-alice' ORDER BY id"):
            print(f"  {r['topic']:<24} {r['file_path']}\n    {r['summary']}")

        print("\n=== tool calls across all runs: JSONL traces in fs9, aggregated with SQL ===")
        q = f"""
        SELECT line->>'tool' AS tool, count(*) AS calls,
               round(avg((line->>'ms')::int)) AS avg_ms,
               sum(CASE WHEN (line->>'ok')::boolean THEN 0 ELSE 1 END) AS errors
        FROM extensions.fs9('/runs/alice-*-{tag}/*.jsonl')
        WHERE line->>'event' = 'tool'
        GROUP BY 1 ORDER BY 2 DESC"""
        for r in c.execute(q):
            print(f"  {r['tool']:<18} calls={r['calls']:<3} avg_ms={r['avg_ms']:<6} errors={r['errors']}")

        print("\n=== reports written by the agent (fs9) ===")
        for r in c.execute("SELECT path, size FROM extensions.fs9('/reports/', recursive => true) WHERE type='file' ORDER BY mtime DESC LIMIT 3"):
            print(f"  {r['path']} ({r['size']} B)")
            print("  " + c.execute("SELECT extensions.fs9_read(%s) AS t", (r["path"],)).fetchone()["t"][:400].replace("\n", "\n  "))
            break

        print("\n=== conversation snapshots (fs9) ===")
        for r in c.execute("SELECT path, size FROM extensions.fs9('/sessions/', recursive => true) "
                           f"WHERE type='file' AND path LIKE '%{tag}%' ORDER BY path"):
            print(f"  {r['path']} ({r['size']} B)")


if __name__ == "__main__":
    main()
