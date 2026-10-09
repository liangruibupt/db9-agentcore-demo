"""Step 5 - bring-your-own embeddings: Amazon Bedrock Titan v2 vectors stored and searched in db9.

db9's built-in embedding() is called inside db9's service account, under db9's daily
token quota. If you need the embedding call under YOUR AWS account (IAM, CloudTrail,
quotas, a specific model), compute vectors with Bedrock yourself and use db9 purely
as the pgvector-compatible store. Same SQL, same HNSW index.

Observed 2026-10-09 (us-west-2): db9's embedding() returns vectors identical to
Titan Text Embeddings v2 (1024-d, normalized) - the script prints the per-chunk
distance between the two columns to show it.
"""
import json
import os
import pathlib
import sys

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from db9_agent.db import connect  # noqa: E402

MODEL = "amazon.titan-embed-text-v2:0"
brt = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def titan(text: str) -> str:
    body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
    vec = json.loads(brt.invoke_model(modelId=MODEL, body=body)["body"].read())["embedding"]
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"  # pgvector text literal


def main():
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kb_chunks_titan (id INT PRIMARY KEY, file_path TEXT, chunk_text TEXT, embedding VECTOR(1024))")
        c.execute("DELETE FROM kb_chunks_titan")
        rows = c.execute("SELECT id, file_path, chunk_text FROM kb_chunks ORDER BY id").fetchall()
        for r in rows:
            c.execute("INSERT INTO kb_chunks_titan VALUES (%s,%s,%s,%s::vector)",
                      (r["id"], r["file_path"], r["chunk_text"], titan(r["chunk_text"])))
        c.execute("CREATE INDEX IF NOT EXISTS kb_titan_hnsw ON kb_chunks_titan USING hnsw (embedding vector_cosine_ops)")
        print(f"embedded {len(rows)} chunks with {MODEL} and stored them in db9")
        d = c.execute("SELECT max(a.embedding <=> b.embedding) AS d FROM kb_chunks a "
                      "JOIN kb_chunks_titan b USING (id)").fetchone()["d"]
        print(f"max cosine distance, db9 embedding() vs Bedrock Titan v2 on the same chunks: {d:.2e}\n")

        for q in ["Do I pay customs duties when shipping to Japan?", "zipper broke after a year"]:
            qv = titan(q)
            # Inline vector literal -> eligible for the HNSW index (bound params are not).
            hits_titan = c.execute(
                f"SELECT file_path, round((embedding <=> '{qv}'::vector)::numeric,4) AS d "
                f"FROM kb_chunks_titan ORDER BY embedding <=> '{qv}'::vector LIMIT 2").fetchall()
            hits_db9 = c.execute(
                "SELECT file_path, round((embedding <=> embedding(%s))::numeric,4) AS d "
                "FROM kb_chunks ORDER BY embedding <=> embedding(%s) LIMIT 2", (q, q)).fetchall()
            print(f"Q: {q}")
            print(f"   Bedrock Titan v2 : {[(h['file_path'], float(h['d'])) for h in hits_titan]}")
            print(f"   db9 embedding()  : {[(h['file_path'], float(h['d'])) for h in hits_db9]}")


if __name__ == "__main__":
    main()
