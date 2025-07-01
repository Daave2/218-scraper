# Amazon Seller Central Scraper

This repository contains an asynchronous Playwright script that logs in to Amazon Seller Central and gathers dashboard metrics for a single store. Results are written to `output/submissions.jsonl` and can optionally be posted to Google Chat via webhook.

The scraper can be run locally or through the included GitHub Actions workflow.

## Table of Contents
- [Features](#features)
- [Requirements](#requirements)
- [Local Setup](#local-setup)
- [Running Locally](#running-locally)
- [GitHub Actions Workflow](#github-actions-workflow)
- [Configuration Reference](#configuration-reference)
- [Notes](#notes)

## Features
- Automates the Seller Central sign‑in flow including two‑step verification
- Collects metrics for a single store configured in `config.json`
- Produces structured logs in `output/` and rotates `app.log`
- Optionally posts metrics to Google Chat using rich cards

## Requirements
- Python 3.11
- Playwright with Chromium browsers
- See `requirements.txt` for the full list of Python packages

## Local Setup
1. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

   To run the included unit tests, also install the development requirements:

   ```bash
   pip install -r requirements-dev.txt
   ```

2. Copy the example configuration and edit it with your credentials:

   ```bash
   cp config.example.json config.json
   # then edit config.json
   ```

   At a minimum provide your Seller Central login details, OTP secret and the `target_store` information.

## Running Locally
Execute the scraper from the command line:

```bash
python scraper.py
```

Logs and scraped data will be saved under the `output/` directory.

## Running Tests
To execute the unit tests locally, make sure the development requirements are installed:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## GitHub Actions Workflow
The workflow in `.github/workflows/run-scraper.yml` installs dependencies, creates
`config.json` from repository secrets and runs the scraper on a schedule. A small
"check-time" job executes every hour and only allows the main scraper job to
continue if the current UK time matches one of the hours listed in the
`UK_TARGET_HOURS` environment variable.

`UK_TARGET_HOURS` is defined near the top of the workflow file and contains a
space-separated list of hours in 24‑hour format:

```yaml
env:
  UK_TARGET_HOURS: '10 14 15 20'
```

In this example the scraper would run at 10:00, 14:00, 15:00 and 20:00 London
time. To change the schedule simply edit this list. For instance, to run only at
09:00 and 17:00 you would set:

```yaml
  UK_TARGET_HOURS: '09 17'
```

Secrets expected by the workflow include `LOGIN_URL`, `LOGIN_EMAIL`, `LOGIN_PASSWORD`, `OTP_SECRET_KEY`, `CHAT_WEBHOOK_URL`, `SUMMARY_CHAT_WEBHOOK_URL`, `TARGET_MERCHANT_ID`, `TARGET_MARKETPLACE_ID` and `TARGET_STORE_NAME`.

Artifacts such as logs are uploaded for each run and kept for seven days.

## Configuration Reference
Key options from `config.example.json`:

- `login_email` / `login_password` – Seller Central credentials
- `otp_secret_key` – secret for generating two‑step verification codes
- `chat_webhook_url` – optional Google Chat webhook for per‑store metrics
- `summary_chat_webhook_url` – optional second webhook for overall metrics only
- `debug` – enable verbose logging and save extra screenshots
- `target_store` – object containing `merchant_id`, `marketplace_id` and `store_name`

See the example file for full details.

## Notes
The repository excludes `config.json`, `state.json` and `output/` from version control. These files may contain sensitive information or large log data. Timestamps recorded by the scraper default to the Europe/London timezone. Modify the `LOCAL_TIMEZONE` constant in `scraper.py` if you prefer a different local time.
