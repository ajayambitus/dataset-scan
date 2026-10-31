"""Scan a local folder (laptop or the review box): python -m dsscan.local_run DIR OUT_DIR [-j N] [--name NAME]

Writes OUT_DIR/rows.jsonl, OUT_DIR/report.json and OUT_DIR/report.md. The EMR job runs the same scan_path per file.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import inventory
from .scan_file import scan_path


def scan_one(args):
    path, rel, workdir = args
    try:
        return list(scan_path(path, rel, workdir))
    except Exception as e:  # noqa: BLE001
        return [{"path": rel, "ext": os.path.splitext(rel)[1].lower(), "size": 0, "kind": "other", "family": "other",
                 "read": "failed", "error": f"{type(e).__name__}: {e}"[:200]}]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("out")
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--name")
    ap.add_argument("--seller", help="JSON file with the seller's inventory answers, keyed like report totals")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="dsscan-")
    jobs = []
    for d, _, names in os.walk(a.root):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p):
                jobs.append((p, os.path.relpath(p, a.root), workdir))
    jobs.sort(key=lambda j: -os.path.getsize(j[0]))  # biggest first so the tail is short
    start = time.time()
    rows = []
    with ProcessPoolExecutor(a.j) as pool:
        futs = [pool.submit(scan_one, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            rows.extend(f.result())
            if i % 500 == 0:
                print(f"{i}/{len(jobs)} files, {time.time() - start:.0f} s", file=sys.stderr)
    with open(os.path.join(a.out, "rows.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    rep = inventory.build(rows, a.name or os.path.basename(os.path.abspath(a.root)))
    rep["scan"] = {"files": len(jobs), "rows": len(rows), "seconds": round(time.time() - start, 1), "workers": a.j}
    seller = json.load(open(a.seller)) if a.seller else None
    json.dump(rep, open(os.path.join(a.out, "report.json"), "w"), indent=2)
    open(os.path.join(a.out, "report.md"), "w").write(inventory.to_markdown(rep, seller))
    print(f"{len(jobs)} files -> {len(rows)} rows in {rep['scan']['seconds']} s; report in {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
