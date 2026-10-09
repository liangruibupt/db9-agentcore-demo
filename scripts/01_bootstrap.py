"""Step 1 - bootstrap: schema, files into fs9, and a RAG index built entirely in SQL.

The interesting part is INGEST_SQL: file listing -> read -> chunk -> embed -> insert,
in ONE statement, inside the database. No S3, no Lambda, no embedding pipeline.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from db9_agent.db import bootstrap, connect  # noqa: E402

INGEST_SQL = """
INSERT INTO kb_chunks (file_path, chunk_index, chunk_text, embedding)
SELECT f.path, c.chunk_index, c.chunk_text, embedding(c.chunk_text)
FROM extensions.fs9('/kb/') AS f
CROSS JOIN LATERAL CHUNK_TEXT(
    content       => extensions.fs9_read(f.path),
    max_chars     => 600,
    overlap_chars => 80,
    title         => f.path
) AS c
WHERE f.type = 'file' AND f.path LIKE '%.md'
"""

ORDERS = [
    ("NG-1001", "u-alice", "Nimbus Trail 2P tent", 329.00, "delivered", "2026-09-02"),
    ("NG-1002", "u-alice", "Merino base layer", 89.00, "delivered", "2026-09-20"),
    ("NG-1003", "u-alice", "Gas canister 4-pack", 24.00, "shipped", "2026-10-05"),
    ("NG-2001", "u-bob", "Summit 45L backpack", 219.00, "delivered", "2026-08-14"),
    ("NG-2002", "u-bob", "Down sleeping bag -5C", 279.00, "processing", "2026-10-07"),
]


def main():
    t0 = time.time()
    with connect() as c:
        bootstrap(c)
        print(f"schema ready ({time.time() - t0:.1f}s)")

        for md in sorted((ROOT / "kb").glob("*.md")):
            n = c.execute("SELECT extensions.fs9_write(%s, %s) AS n", (f"/kb/{md.name}", md.read_text())).fetchone()["n"]
            print(f"  fs9 <- /kb/{md.name} ({n} bytes)")

        c.execute("DELETE FROM kb_chunks")
        t1 = time.time()
        c.execute(INGEST_SQL)
        stats = c.execute("SELECT count(*) AS chunks, count(DISTINCT file_path) AS files FROM kb_chunks").fetchone()
        print(f"in-database ingest: {stats['files']} files -> {stats['chunks']} chunks embedded in {time.time() - t1:.1f}s")

        c.cursor().executemany(
            "INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING", ORDERS
        )
        print(f"orders seeded: {c.execute('SELECT count(*) AS n FROM orders').fetchone()['n']}")

        print("\nfs9 is queryable as a table:")
        for r in c.execute("SELECT path, size FROM extensions.fs9('/kb/') WHERE type='file' ORDER BY path"):
            print(f"  {r['path']:<28} {r['size']:>6} B")
    print(f"\ndone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
