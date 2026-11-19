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

## Deploy v2 on Vercel

Import the repository into Vercel. Both the repository root (the default) and `v2` deploy the v2 FastAPI app. Clear any static build or output overrides. The selected directory configures FastAPI and provides its `app.py` entry point. Connect a Postgres database and expose its connection string as `DATABASE_URL` or `CREDIT_DATABASE_URL` in the production environment before deploying. The connection role needs to create the initial tables in its database. Never commit the connection string.

Vercel deployments use durable Postgres storage, HTTPS-only session cookies, and synthetic cases only. The container and local setup can still use SQLite. Once deployed, open `/`, choose **Try Meridian Packaging**, and reload after saving a review to verify that the case persists.

### Enable email and Google sign-in

The public demo can run without account configuration. Once Auth0 is configured, signing in unlocks statement uploads and replacements. Each workspace is tied to the verified provider account identifier. Guest workspaces stay in that browser's demo session, and signing in with two different providers creates separate workspaces.

1. In Auth0, create a **Regular Web Application**. Enable an email/password Database connection and the **Google** social connection for that application. The default database connection name is `Username-Password-Authentication`. Configure the Google connection with your own OAuth credentials for public production use, and add `https://YOUR_AUTH0_DOMAIN/login/callback` as Google's authorized redirect URI.
2. Set **Allowed Callback URLs** to `https://creditreviewlab-znz5.vercel.app/auth/callback` and **Allowed Logout URLs** to `https://creditreviewlab-znz5.vercel.app/`. Use the same production origin even if Vercel also provides deployment-specific URLs.
3. Add these values to the Vercel project's **Production** environment:

   | Name | Value |
   | --- | --- |
   | `AUTH0_DOMAIN` | Auth0 tenant hostname, with no scheme or slash |
   | `AUTH0_CLIENT_ID` | Auth0 application Client ID |
   | `AUTH0_CLIENT_SECRET` | Auth0 application Client Secret |
   | `CREDIT_SESSION_SECRET` | A randomly generated secret, at least 32 characters. Generate one locally with `py -c "import secrets; print(secrets.token_urlsafe(48))"`. |
   | `CREDIT_APP_ORIGIN` | `https://creditreviewlab-znz5.vercel.app` |
   | `AUTH0_EMAIL_CONNECTION` | Optional override for a differently named email/password database connection |

   Keep both secrets in Vercel's sensitive environment variables. Keep `DATABASE_URL` configured as before.
4. Redeploy, open `/signin` on the production domain, and test both methods with test accounts. A successful sign-in shows **Sign out** and **Upload PDF**. The public demo remains available without an account.

The local workspace still supports uploads without Auth0. The unauthenticated extraction API remains disabled on Vercel. Use synthetic statement packs for portfolio demonstrations.

## Security configuration

See [Security controls and deployment checks](SECURITY.md) for request protections, Auth0 password-change session revocation and the DDL-free PostgreSQL runtime role.

## Team pilot

See [PILOT.md](PILOT.md) for team setup, the private upload worker, database grants, and the checks required before running a real-data pilot. Developer API docs and raw analysis and extraction tools are disabled by default in public deployments. Set `CREDIT_DEVELOPER_API=1` only in controlled development environments.
