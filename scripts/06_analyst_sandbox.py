"""Step 6 - a workload that fits db9 and not Aurora / DynamoDB: the disposable analyst sandbox.

A store owner drops a pile of RAW exports on the platform (CSV, JSONL, a supplier
email) and asks: "Why did our return rate jump in September?"

For this ONE question the platform:
  1. creates a brand-new db9 database (seconds) and drops the raw files into fs9 as-is
  2. hands the agent FULL SQL power inside it: it may CREATE TABLE, join files with each
     other, embed and semantically search review text, write its own report files.
     Blast radius = this throwaway database, so no other job or store is at risk.
  3. reads the report, then deletes the database (or keeps it as an audit artifact).

Why not Aurora: no per-job database in seconds (a new cluster takes minutes and is
billed per instance), no way to SQL-query files that were never loaded (file_fdw is
unavailable on RDS/Aurora; aws_s3 import needs a pre-created table with the right
columns, i.e. a pipeline written BEFORE you know the files), and giving an LLM DDL on a
shared cluster is a blast-radius problem. Why not DynamoDB: no joins, aggregation,
ad-hoc SQL, vectors or files at all.

Honest caveat: for ONE agent analysing files inside ONE session, DuckDB running inside the
AgentCore session's own microVM does this just as well. db9 only pulls ahead when the
sandbox must be a network database shared by several processes / agents, or outlive the
session (see scripts 07 and 08 for the workloads where that is the core requirement).

Usage: python scripts/06_analyst_sandbox.py [--keep]
"""
import csv
import io
import json
import os
import pathlib
import random
import re
import sys
import time
import uuid
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from strands import Agent, tool  # noqa: E402
from strands.models import BedrockModel  # noqa: E402

from db9_agent.db import connect, mkdirs  # noqa: E402
from db9_agent.provision import create_database, delete_database  # noqa: E402
from db9_agent.tools import Tracer, new_run_id  # noqa: E402

QUESTION = "我们店 9 月份的退货率比 8 月高了很多，帮我查清楚原因，量化影响，并给出建议。"
B, R = "\033[1m", "\033[0m"

# --------------------------------------------------------------------------- data
# Synthetic but realistic exports. Planted story: from 2026-09-08 the Summit 45L
# backpack ships from a new supplier batch (B-0908) whose hip-belt buckle snaps.
# Distractor: carrier "QuickShip" got slower in September (complaints, few returns).
PRODUCTS = [("SKU-TENT2P", "Nimbus Trail 2P tent", 329), ("SKU-PACK45", "Summit 45L backpack", 219),
            ("SKU-BAG-5", "Down sleeping bag -5C", 279), ("SKU-MERINO", "Merino base layer", 89),
            ("SKU-STOVE", "Pocket gas stove", 49)]
CARRIERS = ["UPS", "QuickShip", "FedEx"]


def make_exports(seed: int = 7) -> dict[str, str]:
    rnd = random.Random(seed)
    orders, returns, events, reviews = [], [], [], []
    oid = 10000
    for day in range(61):  # 2026-08-01 .. 2026-09-30
        d = date(2026, 8, 1) + timedelta(days=day)
        for _ in range(rnd.randint(14, 22)):
            oid += 1
            sku, name, price = rnd.choices(PRODUCTS, weights=[3, 4, 2, 4, 3])[0]
            carrier = rnd.choice(CARRIERS)
            batch = ("B-0908" if d >= date(2026, 9, 8) else "B-0715") if sku == "SKU-PACK45" else "-"
            orders.append([f"O{oid}", d.isoformat(), sku, name, 1, price, rnd.choice(["WA", "OR", "CA", "BC"]), carrier, batch])
            late = carrier == "QuickShip" and d.month == 9 and rnd.random() < 0.45
            transit = rnd.randint(2, 4) + (rnd.randint(3, 6) if late else 0)
            events.append({"order_id": f"O{oid}", "carrier": carrier, "event": "shipped", "ts": f"{d}T16:00:00Z"})
            events.append({"order_id": f"O{oid}", "carrier": carrier, "event": "delivered",
                           "ts": f"{d + timedelta(days=transit)}T12:00:00Z", **({"note": "delayed at hub"} if late else {})})
            p_ret = 0.04
            reason, comment = rnd.choice([("size", "didn't fit"), ("changed_mind", "no longer needed"),
                                          ("not_as_described", "colour differs from photos")])
            if batch == "B-0908" and rnd.random() < 0.38:
                p_ret = 1.0
                reason, comment = "defective", rnd.choice([
                    "hip belt buckle snapped on first hike", "plastic buckle cracked when tightening",
                    "waist strap clip broke, pack unusable", "buckle shattered in the cold"])
            elif late and rnd.random() < 0.06:
                p_ret = 1.0
                reason, comment = "late_delivery", "arrived after my trip"
            if rnd.random() < p_ret:
                rd = d + timedelta(days=transit + rnd.randint(2, 9))
                returns.append([f"R{oid}", f"O{oid}", rd.isoformat(), reason, comment])
            if rnd.random() < 0.12:
                if batch == "B-0908" and rnd.random() < 0.6:
                    txt, rating = rnd.choice(["Great pack until the hip belt clip broke on day one.",
                                              "The buckle feels cheaper than my old one, it cracked.",
                                              "Returned it - waist buckle failed under load."]), rnd.randint(1, 2)
                elif late:
                    txt, rating = "Product is fine but delivery took forever.", 3
                else:
                    txt, rating = rnd.choice(["Exactly as described, very happy.", "Solid quality for the price.",
                                              "Comfortable and light.", "Fast shipping, good gear."]), rnd.randint(4, 5)
                reviews.append({"order_id": f"O{oid}", "sku": sku, "rating": rating, "text": txt,
                                "date": (d + timedelta(days=transit + 3)).isoformat()})

    def to_csv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue()

    return {
        "/uploads/orders_aug_sep.csv": to_csv(["order_id", "order_date", "sku", "product", "qty", "unit_price",
                                               "region", "carrier", "batch"], orders),
        "/uploads/returns.csv": to_csv(["return_id", "order_id", "return_date", "reason_code", "comment"], returns),
        "/uploads/carrier_events.jsonl": "".join(json.dumps(e) + "\n" for e in events),
        "/uploads/reviews.jsonl": "".join(json.dumps(r) + "\n" for r in reviews),
        "/uploads/notes/supplier-email-2026-09-05.md": (
            "# From: Summit Packs supplier ops\n\nHi team, heads-up: starting with production batch **B-0908** we "
            "switched the hip-belt buckle to a new injection-moulded part from a second vendor to fix lead "
            "times. Same spec on paper. First units ship to you around Sept 8.\n"),
    }


# --------------------------------------------------------------------------- agent
SYSTEM = """You are a data analyst agent working inside your OWN private, throwaway PostgreSQL database (db9).
You have full privileges here: create tables, indexes, views; nothing you do affects anyone else.
The store owner's raw exports are files in the database file system under /uploads (list them with
SELECT path, size FROM extensions.fs9('/uploads/', recursive => true) WHERE type='file').

db9 SQL you can use:
- CSV as a table : SELECT * FROM extensions.fs9('/uploads/x.csv')   -- every column is TEXT, cast as needed
- JSONL as table : SELECT line->>'field' FROM extensions.fs9('/uploads/x.jsonl')   -- line is JSONB
- read a file    : SELECT extensions.fs9_read('/uploads/notes/a.md')
- materialise    : CREATE TABLE t AS SELECT ... FROM extensions.fs9(...)   (do this before heavy joins)
- embeddings     : embedding('text') -> vector(1024); distance: embedding(col) <=> embedding('query')
Work step by step with run_sql, verify numbers, then write a concise markdown report with save_file
to /out/report.md (findings with numbers, root cause, evidence, recommendations), and answer the owner
in their language with the key numbers. All money amounts are USD."""


def build_tools(dsn: str, tracer: Tracer):
    @tool
    def run_sql(sql: str) -> str:
        """Run ANY SQL statement in your private sandbox database (DDL allowed). Returns up to 40 rows as JSON.

        Args:
            sql: one SQL statement
        """
        t0 = time.time()
        try:
            with connect(dsn) as c:
                c.execute("SET statement_timeout = '60s'")
                cur = c.execute(sql)
                rows = cur.fetchmany(40) if cur.description else [{"status": cur.statusmessage}]
            out = json.dumps(rows, ensure_ascii=False, default=str)[:6000]
            tracer.log("tool", tool="run_sql", sql=sql, ms=int((time.time() - t0) * 1000), ok=True)
            return out
        except Exception as e:
            tracer.log("tool", tool="run_sql", sql=sql, ms=int((time.time() - t0) * 1000), ok=False, error=str(e))
            return f"ERROR: {e}"

    @tool
    def save_file(path: str, content: str) -> str:
        """Write a file (e.g. /out/report.md) into the sandbox file system.

        Args:
            path: absolute path under /out/
            content: file content
        """
        if not re.match(r"^/out/[\w./-]+$", path) or ".." in path:
            return "ERROR: path must be under /out/"
        with connect(dsn) as c:
            mkdirs(c, path.rsplit("/", 1)[0])
            c.execute("SELECT extensions.fs9_write(%s, %s)", (path, content))
        tracer.log("tool", tool="save_file", path=path, ok=True)
        return f"saved {path}"

    return [run_sql, save_file]


def main():
    keep = "--keep" in sys.argv
    job = f"job-{uuid.uuid4().hex[:6]}"
    print(f"{B}== 1. new sandbox database for this one question =={R}")
    db = create_database(f"sandbox-{job}")
    dsn = db["dsn"]
    print(f"  db9 create sandbox-{job}: {db['seconds']}s")
    try:
        t1 = time.time()
        files = make_exports()
        with connect(dsn) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS fs9")
            c.execute("CREATE EXTENSION IF NOT EXISTS embedding")
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            for d in ("/uploads/notes", "/out", "/runs"):
                mkdirs(c, d)
            for path, body in files.items():
                c.execute("SELECT extensions.fs9_write(%s, %s)", (path, body))
        for path, body in files.items():
            print(f"  raw file -> {path:<46} {len(body.splitlines()):>5} lines (no schema, no ETL)")
        print(f"  uploaded in {time.time() - t1:.1f}s")

        print(f"\n{B}== 2. agent investigates with full SQL inside its sandbox =={R}\n  owner: {QUESTION}")
        tracer = Tracer(job, new_run_id(), dsn)
        agent = Agent(model=BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
                                         region_name=os.environ.get("AWS_REGION", "us-west-2")),
                      system_prompt=SYSTEM, tools=build_tools(dsn, tracer), callback_handler=None)
        t2 = time.time()
        answer = str(agent(QUESTION))
        print(f"\n  {B}agent ({time.time() - t2:.0f}s):{R}\n  " + answer.replace("\n", "\n  "))

        print(f"\n{B}== 3. what the agent left in its sandbox =={R}")
        with connect(dsn) as c:
            stats = c.execute(f"""
                SELECT count(*) AS calls,
                       sum(CASE WHEN line->>'sql' ~* '^\\s*(create|alter|drop|insert|update)' THEN 1 ELSE 0 END) AS writes,
                       sum(CASE WHEN (line->>'ok')::boolean THEN 0 ELSE 1 END) AS errors
                FROM extensions.fs9('{tracer.path}') WHERE line->>'event' = 'tool'""").fetchone()
            print(f"  tool calls={stats['calls']}  DDL/DML statements={stats['writes']}  errors(self-corrected)={stats['errors']}")
            tables = [r["t"] for r in c.execute(
                "SELECT table_name AS t FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
            print(f"  tables the agent created: {tables}")
            if c.execute("SELECT extensions.fs9_exists('/out/report.md') AS e").fetchone()["e"]:
                rep = c.execute("SELECT extensions.fs9_read('/out/report.md') AS t").fetchone()["t"]
                out = pathlib.Path(os.environ.get("KIROCREW_SCRATCH", "/tmp")) / f"{job}-report.md"
                out.write_text(rep)
                print(f"  /out/report.md ({len(rep)} chars) -> copied to {out}")
    finally:
        if keep:
            print(f"\n  kept sandbox-{job} (delete with: db9 delete sandbox-{job} --yes)")
        else:
            delete_database(f"sandbox-{job}")
            print(f"\n{B}== 4. job done: sandbox-{job} deleted (tables, files, traces) =={R}")


if __name__ == "__main__":
    main()
