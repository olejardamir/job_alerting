#!/usr/bin/env python3
from __future__ import annotations
import csv
from pathlib import Path
from collections import Counter

OUTPUT = Path("output/new_career_pages_validated_full.csv")

sources = [
    "output/new_career_pages_validated_all.csv",
    "output/batch_rem_1.csv",
    "output/batch_rem_2.csv",
]

seen: set[str] = set()
merged: list[dict] = []

for path in sources:
    p = Path(path)
    if not p.exists():
        print(f"Skipping {path} (not found)")
        continue
    with p.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = row.get("record_id", "").strip()
            if rid and rid not in seen:
                seen.add(rid)
                merged.append(row)
            elif rid:
                pass  # duplicate, skip

with OUTPUT.open("w", newline="", encoding="utf-8") as f:
    if merged:
        w = csv.DictWriter(f, fieldnames=merged[0].keys())
        w.writeheader()
        w.writerows(merged)

results = Counter(r.get("validation_result", "UNKNOWN") for r in merged)
print(f"Merged {len(merged)} unique rows to {OUTPUT}")
print(f"\nResults breakdown (from {len(seen)} unique record_ids):")
for k, v in results.most_common():
    print(f"  {k:45s}: {v}")
