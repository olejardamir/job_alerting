#!/bin/bash
cd /home/glompy/Desktop/OTHER_PROJECTS/jobbajobba
source .venv/bin/activate
python validate_career_pages.py \
  --input data/career_validation.csv \
  --output output/career_pages_validated_all.csv \
  --limit 0 \
  --concurrency 3 \
  --tasks-per-minute 30 \
  2>&1
