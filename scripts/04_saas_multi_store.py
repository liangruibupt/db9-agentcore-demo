"""Step 4 - the copilot as multi-store SaaS: one db9 database per store.

Nimbus Gear (outdoor gear) already lives in the default database (script 01).
Two more stores sign up: Peak Cycles (bikes) and Tidepool Surf Co. (surf).

  1. onboard   : each store gets its own database, provisioned with its KB, orders
                 and brand config in seconds (db9 create + one ingest SQL)
  2. serve     : the SAME agent code answers the SAME customer question for all three
                 stores; each answer comes only from that store's own policies/orders
  3. isolate   : prove the data and credentials are physically separate
  4. offboard  : Tidepool cancels -> one call deletes everything it ever stored;
                 its next request is rejected (fail closed)

Usage: python scripts/04_saas_multi_store.py [--keep]   (--keep leaves Peak Cycles onboarded)
"""
import pathlib
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

import psycopg  # noqa: E402

from db9_agent import tenants  # noqa: E402
from db9_agent.agent import handle  # noqa: E402
from db9_agent.db import connect  # noqa: E402

NEW_STORES = ["peak-cycles", "tidepool-surf"]
QUESTION = "我上个月在你们店买的东西已经用过一次了，还能退吗？顺便看看我在你们店都有哪些订单。"
B = "\033[1m"
R = "\033[0m"


def main():
    keep = "--keep" in sys.argv
    print(f"{B}== 1. onboard =={R}")
    for sid in NEW_STORES:
        if sid in tenants.onboarded():
            print(f"  {sid}: already onboarded")
            continue
        s = tenants.onboard(sid)
        print(f"  {s['name']:<18} db={s['db_name']:<22} create={s['create_s']}s provision={s['provision_s']}s "
              f"-> {s['files']} KB files / {s['chunks']} chunks / {s['orders']} orders")

    print(f"\n{B}== 2. same agent, same customer (u-alice), same question, three stores =={R}")
    print(f"  u-alice: {QUESTION}")
    tag = uuid.uuid4().hex[:4]
    for sid in tenants.onboarded():
        out = handle(QUESTION, "u-alice", f"alice-{sid}-{tag}", sid)
        print(f"\n  {B}[{out['store']}]{R} ({out['latency_ms']} ms)\n  " + out["answer"].replace("\n", "\n  "))

    print(f"\n{B}== 3. isolation =={R}")
    for sid in tenants.onboarded():
        with connect(tenants.resolve(sid)) as c:
            orders = [r["order_id"] for r in c.execute("SELECT order_id FROM orders ORDER BY 1")]
            kb = [r["path"] for r in c.execute("SELECT path FROM extensions.fs9('/kb/') WHERE type='file' ORDER BY 1")]
            mem = c.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
        print(f"  {sid:<14} orders={orders}\n  {'':<14} kb={kb} memories={mem}")
    # Credentials are per database: Peak Cycles' password does not open Nimbus Gear's database.
    peak = urlsplit(tenants.resolve("peak-cycles"))
    nimbus_user = urlsplit(tenants.resolve(None)).username
    forged = urlunsplit((peak.scheme, f"{nimbus_user}:{peak.password}@{peak.hostname}:{peak.port}",
                         peak.path, peak.query, ""))
    try:
        psycopg.connect(forged, connect_timeout=10).close()
        print("  !! cross-store login unexpectedly succeeded")
    except psycopg.OperationalError as e:
        reason = str(e).split("FATAL:")[-1].strip().splitlines()[0][:80]
        print(f"  Peak Cycles password against Nimbus Gear's database -> rejected (FATAL: {reason})")

    print(f"\n{B}== 4. offboard =={R}")
    db = tenants.offboard("tidepool-surf")
    print(f"  tidepool-surf cancelled: deleted database {db} (tables, vectors, fs9 files, memories, traces)")
    try:
        handle("还在吗？", "u-alice", f"alice-tidepool-{tag}", "tidepool-surf")
    except tenants.UnknownStore as e:
        print(f"  next request for tidepool-surf -> rejected: {e}")

    if not keep:
        print(f"  demo cleanup: deleted {tenants.offboard('peak-cycles')}")
    print(f"\nonboarded stores now: {tenants.onboarded()}")


if __name__ == "__main__":
    main()
