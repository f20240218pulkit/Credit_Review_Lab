# Credit Review Lab v2

An SME credit file workspace for financial statement spreading, exception review, and versioned evidence. It uses synthetic examples to demonstrate an analyst workflow.

## Run locally

Requires Python 3.11 or later.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python benchmark.py
python -m uvicorn credit_review.api:app
```

Open [the workspace](http://127.0.0.1:8000/) or [the API reference](http://127.0.0.1:8000/docs).

## Demonstrate the revision workflow

1. **Try Meridian Packaging**. Follow the conflicting revenue citations in the exception queue. The source viewer highlights the cited line on a rendered PDF page.
2. Exclude the competing FY26 **Sales revenue** line with a reason. EBITDA margin changes from unavailable to **13.97%**. The separate **₹250,000** balance sheet gap stays open.
3. Optionally confirm the FY26 operating cash flow line from the source ledger. Save a memo snapshot and download its evidence JSON or PDF. Open issues keep the memo in draft.
4. Load the revised Meridian statement. The app carries the unchanged revenue review, reopens the changed cash-flow review if one was recorded, and shows the original and replacement citations. Cash coverage becomes **0.95×**. A 15% cash-flow decline takes it to **0.81×**.
5. Download the earlier memo again. It still references the original document, source values and review history.

## Architecture

FastAPI serves a same-origin browser workspace, typed financial analysis and document routes. PyMuPDF parses bounded text-based statement packs and renders the cited lines. SQLite persists original PDFs, active case state, review history and memo snapshots together on one instance. Cases are scoped to an opaque browser session cookie and updates carry a revision number to detect stale writes. A document hash identifies the original bytes. Each row carries its original page and bounding box, and each ratio preserves the row IDs and formula inputs it used. Decimal strings in the API preserve calculation precision.

A replacement explicitly activates a new statement pack for the same two fiscal years. Reviews carry forward when the source line and its competing account inputs are unchanged. The previous comparison remains tied to the active document versions and original bytes. The memo export captures the reviewed inputs, commentary, scenario, exceptions and review history at export time.

| Supported statement layouts | Sample case |
|---|---|
| Fiscal-year heading with pipe-delimited amounts in thousands | Aster Components |
| FY heading with spaced amount columns in absolute units | Meridian Packaging |
| Reporting-period heading with currency and unit metadata | Juniper Industrial |

Custom local uploads use these three documented text layouts, with up to 2 MiB, 10 pages, 500 lines and two reporting years. The engine calculates in INR. Unsupported currencies are flagged. For schema details, use the synthetic pack download at `GET /cases/{case_id}/statement.pdf` and the interactive API at `/docs`.

## Evaluation

`python benchmark.py` verifies amounts, periods, currencies and page evidence against the three synthetic reference packs. The supported-layout regression matches **60/60** reference rows and verifies **60/60** highlighting coordinates against source text. This measures fixtures for the supported layouts. It provides no accuracy estimate for third-party statements.

The separate 19-label synthetic challenge produces **11/11** correct mappings on eligible unseen labels versus **0/11** for exact aliases. The classifier withholds **6/8** out-of-schema items. Two false suggestions reach review: “Noncurrent asset total” as “Current assets”, and “EBITDA margin percentage” as “EBITDA”. Analyst review remains necessary. Preparation-time savings have not been measured.

To run the standalone browser check against a running local service:

```sh
python -m pip install playwright
python -m playwright install chromium
python tests/browser_smoke.py
```

## Deploy the synthetic demo

```sh
docker build -t credit-review-lab .
docker volume create credit-review-data
docker run --rm -p 8000:8000 -v credit-review-data:/app/data credit-review-lab
```

The container defaults to public demo mode with uploaded PDFs disabled. Use one instance with a persistent volume. Set `CREDIT_SECURE_COOKIE=1` when serving behind HTTPS. The local upload mode is for a trusted local operator and sample data. Multi-tenant production accounts, PostgreSQL, object storage, asynchronous extraction jobs, independently sourced layout evaluation and OCR are outside this release.
