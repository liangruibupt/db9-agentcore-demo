"""Step 3 - the agent gets a sandbox: branch the WHOLE environment, experiment, throw it away.

A db9 branch copies tables, rows, vectors AND the fs9 files (KB, memories, traces).
Scenario: an "ops agent" wants to try a destructive change (re-chunk the knowledge
base with a different chunk size + drop old memories). It does it on a branch, we
compare retrieval quality, and production is never touched.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from db9_agent.db import connect  # noqa: E402

SOURCE_DB = os.environ.get("DB9_DATABASE_NAME", "agentcore-demo")
BRANCH = f"{SOURCE_DB}-exp-{int(time.time()) % 100000}"
DB9 = shutil.which("db9") or os.path.expanduser("~/.local/bin/db9")
QUESTION = "Can I return a tent that I already used camping?"


def db9(*args) -> dict | list:
    out = subprocess.run([DB9, *args, "--json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def top_hit(url: str | None = None) -> str:
    with connect(url) as c:
        r = c.execute(
            "SELECT file_path, round((embedding <=> embedding(%s))::numeric, 4) AS d, left(chunk_text, 90) AS t "
            "FROM kb_chunks ORDER BY embedding <=> embedding(%s) LIMIT 1", (QUESTION, QUESTION)).fetchone()
        n = c.execute("SELECT count(*) AS n FROM kb_chunks").fetchone()["n"]
        m = c.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
    return f"chunks={n:<3} memories={m:<3} top={r['file_path']} d={r['d']}  \"{r['t']}...\""


def main():
    print(f"production   : {top_hit()}")

    t0 = time.time()
    db9("branch", "create", SOURCE_DB, "--name", BRANCH)
    print(f"\nbranch '{BRANCH}' requested in {time.time() - t0:.1f}s (CLONING: full copy incl. fs9 files)")
    while True:
        state = next(d["state"] for d in db9("list") if d["name"] == BRANCH)
        if state == "ACTIVE":
            break
        time.sleep(5)
    print(f"branch ACTIVE after {time.time() - t0:.0f}s")

    url = db9("db", "connect", BRANCH)["connection_string"]  # short-lived (<=15 min) DSN
    try:
        with connect(url) as c:
            files = c.execute("SELECT count(*) AS n FROM extensions.fs9('/', recursive => true) WHERE type='file'").fetchone()["n"]
            print(f"branch has {files} fs9 files copied along with the tables")
            # The risky experiment: wipe and re-chunk with tiny chunks, drop memories.
            c.execute("DELETE FROM memories")
            c.execute("DELETE FROM kb_chunks")
            c.execute("""
                INSERT INTO kb_chunks (file_path, chunk_index, chunk_text, embedding)
                SELECT f.path, ch.chunk_index, ch.chunk_text, embedding(ch.chunk_text)
                FROM extensions.fs9('/kb/') f
                CROSS JOIN LATERAL CHUNK_TEXT(content => extensions.fs9_read(f.path),
                                              max_chars => 200, overlap_chars => 40) ch
                WHERE f.type = 'file'""")
        print(f"\nbranch (exp) : {top_hit(url)}")
        print(f"production   : {top_hit()}   <- untouched")
    finally:
        db9("delete", BRANCH, "--yes")
        print(f"\nbranch '{BRANCH}' deleted")


if __name__ == "__main__":
    main()
