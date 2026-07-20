# Career Validator — Stage 1

This package validates the candidate URLs in `career_validation.csv`.

It does not yet extract all individual job descriptions. Its purpose is to
determine which URLs are usable career pages, ATS pages, single job postings,
false positives, blocked pages, or pages requiring review.

## 1. Create and enter a working folder

```bash
mkdir -p ~/career-monitor
cd ~/career-monitor
```

Copy these files into that folder:

- `validate_career_pages.py`
- `requirements.txt`
- your downloaded `career_validation.csv`

## 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now begin with `(.venv)`.

## 3. Install the dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
playwright install chromium
```

On Ubuntu or Debian, if Chromium reports missing system libraries:

```bash
sudo .venv/bin/playwright install-deps chromium
```

Then repeat:

```bash
playwright install chromium
```

## 4. Confirm installation

```bash
python -c "import crawlee; print(crawlee.__version__)"
python -c "from playwright.sync_api import sync_playwright; print('Playwright import OK')"
```

## 5. Run a visible five-page test

```bash
python validate_career_pages.py \
  --input career_validation.csv \
  --output output/test_5.csv \
  --limit 5 \
  --headful \
  --concurrency 1 \
  --tasks-per-minute 10
```

A Chromium window will open. The output will be:

```text
output/test_5.csv
```

Open that file and inspect:

- `loaded_url`
- `http_status`
- `page_title`
- `detected_ats`
- `jsonld_jobposting_count`
- `probable_job_link_count`
- `validation_result`
- `validation_reason`
- `crawl_error`

## 6. Run a 25-page pilot

After the five-page run works:

```bash
rm -rf storage
python validate_career_pages.py \
  --input career_validation.csv \
  --output output/pilot_25.csv \
  --limit 25 \
  --concurrency 2 \
  --tasks-per-minute 20
```

## 7. Run the whole file

Do this only after checking the pilot:

```bash
rm -rf storage
python validate_career_pages.py \
  --input career_validation.csv \
  --output output/career_pages_validated_all.csv \
  --limit 0 \
  --concurrency 3 \
  --tasks-per-minute 30
```

`--limit 0` means all rows.

## Important operational rules

1. Do not start with all 4,529 rows.
2. Do not use high concurrency initially.
3. Remove `storage/` before a clean rerun, or the default dataset may retain
   results from a previous interrupted run.
4. The output order may differ from the input order. Use `record_id` to join
   records.
5. A result of `needs_manual_review` is not failure. It means page evidence was
   inconclusive.
6. A result of `crawl_failed` proceeds later to the inaccessible retry stage.
7. No LLM is used in this stage.
