"""EMR Serverless (Spark) entry point: scan every object under an S3 prefix and write a report card.

  job.py --input s3://bucket/prefix/ --output s3://ambitus-assets/review-runs/_emr/scans/<run>/ [--name NAME]
         [--max-bytes 4000000000] [--slices N]

Each Spark task downloads its files to local disk, runs dsscan.scan_path (archives are unpacked and their members
scanned), deletes the copies, and returns small rows (no file contents, PII counts only). Files over --max-bytes are
scanned from their first 64 MB only (row marked partial). The driver writes rows.jsonl, report.json and report.md.
"""
import argparse
import json
import os
import shutil
import tempfile
import time

import boto3
from pyspark.sql import SparkSession

HEAD_BYTES = 64 * 1024 * 1024


def split_s3(url):
    assert url.startswith("s3://"), url
    bucket, _, key = url[5:].partition("/")
    return bucket, key


def list_objects(bucket, prefix):
    s3 = boto3.client("s3")
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            if not o["Key"].endswith("/"):
                out.append((o["Key"], o["Size"], o.get("ETag", "").strip('"')))
    return out


def balance(objs, n):
    """Greedy: biggest files first, each to the lightest bucket, so no task gets all the big ones."""
    buckets = [[0, []] for _ in range(n)]
    for o in sorted(objs, key=lambda o: -o[1]):
        b = min(buckets, key=lambda b: b[0])
        b[0] += o[1]
        b[1].append(o)
    return [b[1] for b in buckets if b[1]]


def scan_batch(batch, bucket, prefix, max_bytes):
    from dsscan.scan_file import scan_path, scan_file
    s3 = boto3.client("s3")
    work = tempfile.mkdtemp(prefix="dsscan-")
    try:
        for key, size, etag in batch:
            rel = key[len(prefix):].lstrip("/") or os.path.basename(key)
            local = os.path.join(work, "obj" + os.path.splitext(key)[1][:12])
            t0 = time.time()
            try:
                if size > max_bytes:
                    body = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{HEAD_BYTES - 1}")["Body"].read()
                    with open(local, "wb") as f:
                        f.write(body)
                    rows = [scan_file(local, rel, partial=True, size=size)]
                else:
                    s3.download_file(bucket, key, local)
                    rows = list(scan_path(local, rel, work))
            except Exception as e:  # noqa: BLE001 - one bad object never fails the job
                rows = [{"path": rel, "ext": os.path.splitext(rel)[1].lower() or "(none)", "size": size,
                         "kind": "other", "family": "other", "read": "failed", "error": f"{type(e).__name__}: {e}"[:200]}]
            finally:
                if os.path.exists(local):
                    os.remove(local)
            rows[0]["etag"] = etag
            rows[0]["scan_s"] = round(time.time() - t0, 2)
            for r in rows:
                yield json.dumps(r)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--name")
    ap.add_argument("--max-bytes", type=int, default=4_000_000_000)
    ap.add_argument("--slices", type=int, default=0, help="number of tasks (default: about 4 per core)")
    a = ap.parse_args()

    spark = SparkSession.builder.appName("dsscan").getOrCreate()
    sc = spark.sparkContext
    bucket, prefix = split_s3(a.input)
    out_bucket, out_prefix = split_s3(a.output.rstrip("/") + "/")
    t0 = time.time()
    objs = list_objects(bucket, prefix)
    listed = time.time() - t0
    n = a.slices or max(8, min(len(objs), sc.defaultParallelism * 4))
    batches = balance(objs, n)
    print(f"dsscan: {len(objs)} objects, {sum(o[1] for o in objs) / 1e9:.2f} GB, {len(batches)} tasks, "
          f"listed in {listed:.1f} s", flush=True)

    rows_rdd = sc.parallelize(batches, len(batches)).flatMap(
        lambda batch: scan_batch(batch, bucket, prefix, a.max_bytes))
    lines = rows_rdd.collect()  # rows are small (hundreds of bytes); fine into the millions
    scanned = time.time() - t0

    from dsscan import inventory
    rows = [json.loads(x) for x in lines]
    rep = inventory.build(rows, a.name or prefix.rstrip("/").split("/")[-1] or bucket)
    rep["scan"] = {"objects": len(objs), "rows": len(rows), "gb": round(sum(o[1] for o in objs) / 1e9, 3),
                   "list_seconds": round(listed, 1), "total_seconds": round(scanned, 1), "tasks": len(batches),
                   "input": a.input}
    s3 = boto3.client("s3")
    put = dict(Bucket=out_bucket, ServerSideEncryption="AES256")
    s3.put_object(Key=out_prefix + "rows.jsonl", Body=("\n".join(lines) + "\n").encode(), **put)
    s3.put_object(Key=out_prefix + "report.json", Body=json.dumps(rep, indent=2).encode(), **put)
    s3.put_object(Key=out_prefix + "report.md", Body=inventory.to_markdown(rep).encode(), **put)
    print(f"dsscan: done in {scanned:.1f} s -> {a.output}", flush=True)
    spark.stop()


if __name__ == "__main__":
    main()
