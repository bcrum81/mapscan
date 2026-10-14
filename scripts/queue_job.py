#!/usr/bin/env python3
"""Queue a worker job from the command line.

The dashboard can do all of this; this exists for ops and for watching a long
scan from a terminal.

    python -m scripts.queue_job scan https://www.example.com/
    python -m scripts.queue_job recheck
    python -m scripts.queue_job discovery
    python -m scripts.queue_job watch 7          # follow job 7 until it ends
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arq.connections import RedisSettings, create_pool  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402

KINDS = {
    "scan": ("domain_scan", "domain_scan"),
    "recheck": ("recheck", "recheck_all"),
    "discovery": ("discovery_sweep", "discovery_sweep"),
    "ebay": ("ebay_sweep", "ebay_sweep"),
}


async def queue(kind: str, target: str | None) -> int:
    db_kind, fn = KINDS[kind]
    await db.init_pool()
    job_row_id = await db.fetchval(
        "INSERT INTO crawl_jobs (kind, target, status) VALUES ($1,$2,'queued') "
        "RETURNING id",
        db_kind, target or "cli trigger",
    )
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    if kind == "scan":
        job = await redis.enqueue_job(fn, job_row_id, target)
    else:
        job = await redis.enqueue_job(fn, job_row_id)
    await db.execute("UPDATE crawl_jobs SET arq_job_id=$2 WHERE id=$1",
                     job_row_id, job.job_id if job else None)
    await redis.aclose()
    await db.close_pool()
    print(f"queued job {job_row_id} ({db_kind})" + (f" -> {target}" if target else ""))
    return job_row_id


async def watch(job_row_id: int, interval: float = 4.0) -> int:
    await db.init_pool()
    last = None
    while True:
        row = await db.fetchrow(
            "SELECT status, progress, error, finished_at FROM crawl_jobs WHERE id=$1",
            job_row_id,
        )
        if row is None:
            print(f"job {job_row_id} not found")
            await db.close_pool()
            return 1
        prog = row["progress"]
        if isinstance(prog, str):
            prog = json.loads(prog)
        line = f"[{row['status']}] {json.dumps(prog, sort_keys=True)}"
        if line != last:
            print(line, flush=True)
            last = line
        if row["status"] in ("done", "failed", "cancelled"):
            if row["error"]:
                print(f"ERROR: {row['error']}")
            await db.close_pool()
            return 0 if row["status"] == "done" else 1
        await asyncio.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=[*KINDS, "watch"])
    ap.add_argument("target", nargs="?", default=None,
                    help="URL for scan, job id for watch")
    ap.add_argument("--follow", action="store_true",
                    help="queue then watch until the job ends")
    args = ap.parse_args()

    if args.action == "watch":
        if not args.target:
            print("watch needs a job id", file=sys.stderr)
            return 2
        return asyncio.run(watch(int(args.target)))

    if args.action == "scan" and not args.target:
        print("scan needs a URL", file=sys.stderr)
        return 2

    job_id = asyncio.run(queue(args.action, args.target))
    if args.follow:
        return asyncio.run(watch(job_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
