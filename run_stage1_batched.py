#!/usr/bin/env python3
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

INPUT = Path("data/new_career_validation.csv")
BATCH_SIZE = 50
VENV_PY = Path(".venv/bin/python")
SCRIPT = Path("validate_career_pages.py")
OUTPUT_DIR = Path("output")
STORAGE = Path("storage")

rows = []
with INPUT.open(newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        rows.append(row)

total = len(rows)
print(f"Total rows: {total}, batch size: {BATCH_SIZE}")

all_outputs: list[Path] = []
for i in range(0, total, BATCH_SIZE):
    batch = rows[i:i+BATCH_SIZE]
    batch_file = OUTPUT_DIR / f"batch_{i//BATCH_SIZE}.csv"
    with batch_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(batch)
    
    out_file = OUTPUT_DIR / f"batch_out_{i//BATCH_SIZE}.csv"
    jsonl_file = OUTPUT_DIR / f"batch_out_{i//BATCH_SIZE}.jsonl"
    
    # Clean storage before each batch
    if STORAGE.exists():
        subprocess.run(["rm", "-rf", str(STORAGE)])
    
    cmd = [
        str(VENV_PY), str(SCRIPT),
        "--input", str(batch_file),
        "--output", str(out_file),
        "--limit", "0",
        "--concurrency", "3",
        "--tasks-per-minute", "30",
    ]
    print(f"\nBatch {i//BATCH_SIZE + 1}/{(total+BATCH_SIZE-1)//BATCH_SIZE}: rows {i+1}-{min(i+BATCH_SIZE, total)}")
    sys.stdout.flush()
    
    result = subprocess.run(cmd, timeout=300)
    if result.returncode:
        print(f"  Batch failed (exit {result.returncode}), continuing")
    else:
        all_outputs.append(out_file)
        print(f"  Completed: {out_file}")

# Merge all outputs
print("\n\nMerging outputs...")
with (OUTPUT_DIR / "new_career_pages_validated_all.csv").open("w", newline="", encoding="utf-8") as fout:
    first = True
    for out_file in all_outputs:
        if not out_file.exists():
            continue
        with out_file.open(newline="", encoding="utf-8") as fin:
            reader = csv.reader(fin)
            header = next(reader)
            if first:
                fout.write(",".join(header) + "\n")
                first = False
            for row in reader:
                fout.write(",".join(row) + "\n")
