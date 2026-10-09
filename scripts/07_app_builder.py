"""Step 7 - an app-builder agent platform (Replit / Lovable / Bolt style) on db9.

A user types one sentence ("build me a camping-gear rental booking system"). The
platform gives that app its OWN database in seconds, and a builder agent (Bedrock
Claude) designs the schema, runs the migrations itself, seeds data, writes the app's
API as named SQL queries, and tests every endpoint - all inside the app's database.
The generated source (schema.sql, seed.sql, api.json, README.md) is stored next to the
data in the same database's file system, so "the app" is one self-contained unit.

Lifecycle shown:
  1. build     : two apps built in parallel, each in its own fresh database
  2. verify    : the platform runs every generated endpoint (inside a rolled-back txn)
  3. iterate   : user asks for a change -> branch the app (tables + data + source files),
                 the agent migrates the branch, the platform re-verifies, then promotes
                 the migration to the live app and deletes the branch
  4. abandon   : most prototypes die; the other app is deleted with one call

Why db9 and not Aurora / DynamoDB: an app platform creates databases by the thousands,
most of them idle or abandoned within hours, each needing its own credentials that can be
handed to the user. One Aurora cluster per app takes minutes and has a floor cost plus a
regional quota; packing them into shared clusters breaks per-app credentials, deletion,
branching and noisy-neighbour isolation. DynamoDB is not the Postgres that generated code
expects.

Usage: python scripts/07_app_builder.py [--keep]   (--keep leaves the iterated app running)
"""
import json
import os
import pathlib
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from strands import Agent, tool  # noqa: E402
from strands.models import BedrockModel  # noqa: E402

from db9_agent.db import connect, mkdirs  # noqa: E402
from db9_agent.provision import branch_database, create_database, delete_database  # noqa: E402

B, R = "\033[1m", "\033[0m"
APPS = {
    "camp-rental": "做一个露营装备租赁预约系统：每种装备有库存数量和日租金；顾客按日期区间预约若干件，"
                   "同一时间段不能超订；要能查询某个日期区间还有哪些装备、各剩几件可租，以及顾客的预约列表和总价。",
    "run-club": "做一个社区跑团活动报名系统：每个活动有人数上限；会员可以报名和取消；满员后进入候补名单，"
                "有人取消时候补第一位自动转为正式报名；要能查看活动的报名名单和候补名单。",
}
CHANGE_REQUEST = ("新需求：每种装备增加押金金额（deposit_usd）。预约时记录本次预约的押金总额（件数 × 押金），"
                  "查询顾客预约列表的接口要同时返回租金总价和押金总额。已有数据不能丢，已有接口要继续能用。")

BUILDER_PROMPT = """You are the builder agent of an app platform. You are building ONE app inside its own empty,
private PostgreSQL-compatible database (db9). You have full privileges; nothing else lives in this database.

Deliverables (use the tools; run everything for real, do not just describe it):
1. Design the schema and APPLY it with run_sql. Enforce business rules in the database where you can
   (constraints, triggers or functions). db9 is PostgreSQL-compatible but not 100%: if a feature errors,
   pick another approach and keep going.
2. Insert realistic seed data (a few rows per table).
3. Write the app API as named parameterised queries. Parameters use psycopg named style, e.g. %(gear_id)s.
   Each endpoint must be ONE SQL statement. TEST every endpoint with test_sql (it rolls back, so tests never
   change the seed data); use run_sql only for schema and seed changes you want to keep.
4. Save these files with write_file:
   /app/schema.sql   - the exact DDL you applied
   /app/seed.sql     - the seed inserts
   /app/api.json     - JSON array of {"name", "description", "sql", "example_params": {...}}
                       (example_params must make the endpoint succeed against the seed data)
   /app/README.md    - short description of the app and its endpoints
Finish with a 3-line summary of what you built."""

ITERATE_PROMPT = """You are the builder agent of an app platform, working on a BRANCH of an existing app's database
(PostgreSQL-compatible db9, full privileges). The app's source is in the database file system:
/app/schema.sql, /app/seed.sql, /app/api.json, /app/README.md (read them with read_file first).

Implement the change request below as a forward-only migration:
1. Write the migration SQL, APPLY it with run_sql, and make sure existing rows stay valid (backfill if needed).
2. Save it as /app/migrations/002_<short-name>.sql (the platform will replay this exact file on the live app).
3. Update /app/api.json and TEST every endpoint with test_sql (rolls back). Existing clients must keep working:
   keep every existing endpoint name, its parameters and every column it returns (you may ADD columns, never
   rename or remove them). Update /app/schema.sql and /app/README.md to reflect the new state.
Finish with a 3-line summary.

Change request: """


def builder_tools(dsn: str, log: list):
    @tool
    def run_sql(sql: str) -> str:
        """Run ANY single SQL statement in the app's private database (DDL allowed). Returns up to 30 rows as JSON.

        Args:
            sql: one SQL statement
        """
        try:
            with connect(dsn) as c:
                c.execute("SET statement_timeout = '60s'")
                cur = c.execute(sql)
                rows = cur.fetchmany(30) if cur.description else [{"status": cur.statusmessage}]
            log.append(("sql", True))
            return json.dumps(rows, ensure_ascii=False, default=str)[:5000]
        except Exception as e:
            log.append(("sql", False))
            return f"ERROR: {e}"

    @tool
    def test_sql(sql: str, params_json: str = "{}") -> str:
        """Test ONE statement (e.g. a write endpoint) inside a transaction that is ROLLED BACK afterwards.

        Args:
            sql: one SQL statement, may use psycopg named params like %(gear_id)s
            params_json: JSON object with the parameter values
        """
        try:
            with connect(dsn) as c:
                c.autocommit = False
                cur = c.execute(sql, json.loads(params_json or "{}") or None)
                rows = cur.fetchmany(30) if cur.description else [{"status": cur.statusmessage}]
                c.rollback()
            log.append(("sql", True))
            return "ROLLED BACK. " + json.dumps(rows, ensure_ascii=False, default=str)[:5000]
        except Exception as e:
            log.append(("sql", False))
            return f"ERROR: {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write an app source file under /app/ in the database file system.

        Args:
            path: absolute path under /app/
            content: file content
        """
        if not re.match(r"^/app/[\w./-]+$", path) or ".." in path:
            return "ERROR: path must be under /app/"
        with connect(dsn) as c:
            mkdirs(c, path.rsplit("/", 1)[0])
            c.execute("SELECT extensions.fs9_write(%s, %s)", (path, content))
        log.append(("file", True))
        return f"saved {path}"

    @tool
    def read_file(path: str) -> str:
        """Read an app source file from the database file system.

        Args:
            path: absolute path, e.g. /app/api.json
        """
        with connect(dsn) as c:
            if not c.execute("SELECT extensions.fs9_exists(%s) AS e", (path,)).fetchone()["e"]:
                return "ERROR: not found"
            return c.execute("SELECT extensions.fs9_read(%s) AS t", (path,)).fetchone()["t"]

    return [run_sql, test_sql, write_file, read_file]


def run_builder(dsn: str, prompt: str) -> dict:
    log: list = []
    agent = Agent(model=BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
                                     region_name=os.environ.get("AWS_REGION", "us-west-2")),
                  system_prompt=BUILDER_PROMPT if not prompt.startswith(ITERATE_PROMPT) else ITERATE_PROMPT,
                  tools=builder_tools(dsn, log), callback_handler=None)
    t0 = time.time()
    summary = str(agent(prompt))
    return {"summary": summary.strip(), "seconds": round(time.time() - t0),
            "sql_ok": sum(1 for k, ok in log if k == "sql" and ok), "sql_err": sum(1 for k, ok in log if k == "sql" and not ok)}


def verify(dsn: str) -> list[tuple[str, bool, str, tuple]]:
    """Platform-side acceptance test: run every generated endpoint with its example params, then roll back.
    Returns (name, ok, message, returned column names)."""
    results = []
    with connect(dsn) as c:
        if not c.execute("SELECT extensions.fs9_exists('/app/api.json') AS e").fetchone()["e"]:
            return [("api.json", False, "missing", ())]
        api = json.loads(c.execute("SELECT extensions.fs9_read('/app/api.json') AS t").fetchone()["t"])
    for ep in api:
        try:
            with connect(dsn) as c:
                c.autocommit = False
                cur = c.execute(ep["sql"], ep.get("example_params") or {})
                cols = tuple(d.name for d in cur.description) if cur.description else ()
                n = len(cur.fetchall()) if cur.description else cur.rowcount
                c.rollback()
            results.append((ep["name"], True, f"{n} rows", cols))
        except Exception as e:
            results.append((ep["name"], False, str(e).splitlines()[0][:90], ()))
    return results


def compat(old: list, new: list) -> list[tuple[str, bool, str, tuple]]:
    """Backward compatibility: every old endpoint still exists and still returns every old column."""
    by_name = {n: cols for n, ok, _, cols in new if ok}
    out = []
    for name, ok, _, cols in old:
        if name not in by_name:
            out.append((name, False, "endpoint removed or failing", ()))
        else:
            missing = sorted(set(cols) - set(by_name[name]))
            out.append((name, not missing, f"missing columns {missing}" if missing else "compatible", ()))
    return out


def describe(dsn: str) -> str:
    with connect(dsn) as c:
        tables = [r["t"] for r in c.execute(
            "SELECT table_name AS t FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
        files = [r["path"] for r in c.execute(
            "SELECT path FROM extensions.fs9('/app/', recursive => true) WHERE type='file' ORDER BY 1")]
    return f"tables={tables}\n      source files in the same database: {files}"


def show_verify(results):
    for name, ok, msg, _ in results:
        print(f"      {'PASS' if ok else 'FAIL'}  {name:<34} {msg}")
    return sum(r[1] for r in results), len(results)


def main():
    keep = "--keep" in sys.argv
    tag = uuid.uuid4().hex[:4]
    dbs: dict[str, dict] = {}
    branch = None
    try:
        print(f"{B}== 1. two users, two one-sentence requests -> two apps, each with its own database =={R}")
        for app in APPS:
            dbs[app] = create_database(f"app-{app}-{tag}")
            with connect(dbs[app]["dsn"]) as c:  # fs9 must be enabled before fs9() can be used as a table
                c.execute("CREATE EXTENSION IF NOT EXISTS fs9")
            print(f"  {app:<12} db={dbs[app]['name']:<22} created in {dbs[app]['seconds']}s")
        with ThreadPoolExecutor(len(APPS)) as pool:
            builds = dict(zip(APPS, pool.map(lambda a: run_builder(dbs[a]["dsn"], APPS[a]), APPS)))

        print(f"\n{B}== 2. platform verifies every generated endpoint =={R}")
        for app, b in builds.items():
            print(f"\n  {B}{app}{R}: built in {b['seconds']}s, {b['sql_ok']} SQL ok / {b['sql_err']} errors fixed along the way")
            print("    " + b["summary"].replace("\n", "\n    "))
            print(f"    {describe(dbs[app]['dsn'])}")
            p, n = show_verify(verify(dbs[app]["dsn"]))
            print(f"    -> {p}/{n} endpoints pass")

        app = "camp-rental"
        print(f"\n{B}== 3. iterate on {app}: branch -> migrate -> verify -> promote =={R}\n  user: {CHANGE_REQUEST}")
        live = verify(dbs[app]["dsn"])
        branch = branch_database(dbs[app]["name"], f"{dbs[app]['name']}-dev")
        print(f"  branch {branch['name']} ACTIVE in {branch['seconds']}s (tables + data + /app source files copied)")
        it = run_builder(branch["dsn"], ITERATE_PROMPT + CHANGE_REQUEST)
        print(f"  agent migrated the branch in {it['seconds']}s:\n    " + it["summary"].replace("\n", "\n    "))
        new = verify(branch["dsn"])
        p, n = show_verify(new)
        print(f"    -> branch: {p}/{n} endpoints pass; backward compatibility with the live app's clients:")
        cp, cn = show_verify(compat(live, new))
        p, n = p + cp, n + cn
        with connect(branch["dsn"]) as c:
            mig = c.execute("SELECT path FROM extensions.fs9('/app/migrations/') WHERE type='file' ORDER BY 1").fetchall()
            files = {r["path"]: c.execute("SELECT extensions.fs9_read(%s) AS t", (r["path"],)).fetchone()["t"]
                     for r in c.execute("SELECT path FROM extensions.fs9('/app/', recursive => true) WHERE type='file'")}
        if p == n and mig:
            # Promote: replay the migration on the live app, then ship the updated source files.
            with connect(dbs[app]["dsn"]) as c:
                c.execute(files[mig[-1]["path"]])
                for path, body in files.items():
                    mkdirs(c, path.rsplit("/", 1)[0])
                    c.execute("SELECT extensions.fs9_write(%s, %s)", (path, body))
            print(f"  promoted {mig[-1]['path']} to the live app")
            p2, n2 = show_verify(verify(dbs[app]["dsn"]))
            print(f"    -> live: {p2}/{n2} endpoints pass")
        else:
            print("  branch did not pass the gate; live app untouched")
    finally:
        if branch:
            delete_database(branch["name"])
            print(f"\n  branch {branch['name']} deleted")
        for app, db in dbs.items():
            if keep and app == "camp-rental":
                print(f"  kept {db['name']} (delete with: db9 delete {db['name']} --yes)")
                continue
            delete_database(db["name"])
            print(f"  {B}== 4.{R} {app}: deleted {db['name']} (prototype abandoned / demo cleanup)")


if __name__ == "__main__":
    main()
