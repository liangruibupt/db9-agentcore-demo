"""Step 1 - bootstrap the first store (Nimbus Gear) in the default database.

Schema, brand config + KB files into fs9, and a RAG index built entirely in SQL
(db.INGEST_SQL: file listing -> read -> chunk -> embed -> insert, ONE statement,
inside the database. No S3, no Lambda, no embedding pipeline).
Script 04 onboards more stores the same way, each into its own database.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from db9_agent.db import connect, provision_store  # noqa: E402
from db9_agent.tenants import DEFAULT_STORE, STORES_DIR  # noqa: E402


def main():
    t0 = time.time()
    with connect() as c:
        s = provision_store(c, STORES_DIR / DEFAULT_STORE)
        print(f"store '{s['name']}': {s['files']} KB files -> {s['chunks']} chunks embedded in SQL, "
              f"{s['orders']} orders ({time.time() - t0:.1f}s)")

        print("\nfs9 is queryable as a table:")
        for r in c.execute("SELECT path, size FROM extensions.fs9('/', recursive => true) "
                           "WHERE type='file' AND path ~ '^/(kb|config)/' ORDER BY path"):
            print(f"  {r['path']:<28} {r['size']:>6} B")
    print(f"\ndone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
