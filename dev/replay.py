#!/usr/bin/env python3
"""Replay the detectors over a Hermes session database (read-only) and print aggregates.

    python3 dev/replay.py --db ~/.hermes/state.db --days 90 --samples 10

This is the verification step behind every rule marked ``verified``: per-rule hit counts, how often
the model recovered, how many turns would have ended with a card, and N random hits per rule as
metadata (session prefix, row id, numbers) for a manual look. It prints no message content.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fine_print import rules  # noqa: E402,F401
from fine_print.engine import REGISTRY  # noqa: E402
from fine_print.report import render_report, scan  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(Path.home() / ".hermes" / "state.db"))
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--samples", type=int, default=0, help="random hits per rule to print (metadata only)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    samples: dict[str, list] = {}
    counts: dict[str, int] = {}
    rng = random.Random(args.seed)

    def keep(sid, mid, obs):          # reservoir sampling
        n = counts[obs.rule_id] = counts.get(obs.rule_id, 0) + 1
        row = (sid[:12], mid, obs.subject[:40], dict(obs.numbers), obs.recovered, obs.severity)
        bucket = samples.setdefault(obs.rule_id, [])
        if len(bucket) < args.samples:
            bucket.append(row)
        elif args.samples and rng.randrange(n) < args.samples:
            bucket[rng.randrange(args.samples)] = row

    t0 = time.time()
    result = scan(REGISTRY, Path(args.db).expanduser(), time.time() - args.days * 86400, on_obs=keep)
    print(render_report(result, args.days))
    print(f"\nscan: {time.time() - t0:.1f}s · cards {result['cards']:,} in {result['card_turns']:,} turns "
          f"· turns {result['turns']:,} · turns with tools {result['tool_turns']:,}")
    print("\nper rule: hits / recovered in turn / recovered by session end / would card")
    for rule_id, records in sorted(result["rules"].items()):
        rec = sum(1 for r in records if r["recovered"])
        later = sum(1 for r in records if r["later"])
        cards = sum(1 for r in records if r.get("card"))
        status = getattr(REGISTRY.get(rule_id), "status", "?")
        print(f"  {rule_id:<40} {len(records):>6} / {rec:>5} / {rec + later:>5} / {cards:>5}   [{status}]")
    if REGISTRY.failed:
        print("\nRULE FAILURES:", REGISTRY.failed)
    for rule_id, rows in sorted(samples.items()):
        print(f"\nsamples · {rule_id}")
        for row in rows:
            print("  ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
