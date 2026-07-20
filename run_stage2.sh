#!/bin/bash
cd /home/glompy/Desktop/OTHER_PROJECTS/jobbajobba
source .venv/bin/activate
python resolve_probable_career_pages.py \
  --input probable_career_pages.csv \
  --output output/stage2_probable_resolved_all.csv \
  --jsonl output/stage2_probable_resolved_all.jsonl \
  --limit 0 \
  --concurrency 2 \
  --tasks-per-minute 20 \
  2>&1
