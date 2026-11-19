# Credit Review Lab pilot setup

The deployed public workspace offers synthetic demos and Help. The signed-in application supports personal cases, team invitations, an archiveable case inbox, CSV fallback, statement replacements, and a second reviewer’s sign-off. Use consented or synthetic data until the launch checks below are complete.

## Provision staging

1. Use a separate Auth0 application, database, and file bucket for staging. Configure Auth0 using [README.md](README.md) and [SECURITY.md](SECURITY.md). The account implementing team invitations must sign in with an Auth0 connection that supplies a verified email. Users with an older browser session should sign in again before accepting an invitation.
2. Set `CREDIT_PILOT_INVITE_ONLY=1` and `CREDIT_PILOT_ADMINS` to the comma-separated verified email addresses permitted to create pilot teams. In this mode uploads and replacements require team membership and an analyst or administrator role. Enable this mode on a fresh pilot database or inventory existing organizations before enabling it. Out-of-band invitation links expire after 48 hours, are bound to the verified invitee email, and can be used once. Share them through your approved internal channel.
3. Apply the additive schema before pointing the deployment at a restricted role. Existing personal and demo case IDs, PDFs, and snapshots stay in their original scope. With a PostgreSQL migration credential and the checkout dependencies installed, run

   ```sh
   python -c 'import os; from credit_review.workspace import init_postgres; init_postgres(os.environ["CREDIT_DATABASE_URL"])'
   ```

   With `CREDIT_DB_MANAGED_SCHEMA=1`, the application assumes the tables are already provisioned. Add these grants to the grants in SECURITY.md, adjusting role and schema names to match the deployment.

   ```sql
   GRANT SELECT, INSERT ON organizations, team_cases TO credit_review_runtime;
   GRANT SELECT, INSERT, DELETE ON memberships TO credit_review_runtime;
   GRANT SELECT, INSERT, UPDATE ON invitations, intake_jobs TO credit_review_runtime;
   GRANT SELECT, INSERT, UPDATE, DELETE ON usage_windows TO credit_review_runtime;
   GRANT SELECT, INSERT ON original_uploads, object_sources TO credit_review_runtime;
   ```

   The pilot uses application-enforced membership plus per-case transaction locks. PostgreSQL row-level security is still a launch gap for enterprise use.

## Enable the private worker path

1. Provision a private AWS S3 bucket with versioning enabled, public access blocked, and encryption at rest. Use a region approved for the data. Configure bucket CORS for only the applicable HTTPS workspace origin and POST method. Upload authorization has a five-minute lifetime. The worker checks the stored version, content and checksum before exposing the evidence. Approved files remain pinned to their uploaded version.
2. Set `CREDIT_UPLOAD_BUCKET`, `CREDIT_UPLOAD_ORIGIN` (the exact HTTPS origin used by the bucket's presigned POST form), and `AWS_REGION` in the web environment. Use the AWS SDK credential chain with a scoped service identity for that bucket prefix. The website needs the ability to sign authorized uploads and to read the versioned source files. The worker needs versioned reads and database access. Separate identities and database credentials wherever supported.
3. Run `python -m credit_review.intake_worker` as a long-running service with the same release, bucket configuration, and PostgreSQL schema as the web app. `--once` is available for a controlled smoke test. The HTTP service queues new and replacement team uploads and presents their status. Leases and attempt fencing prevent a restarted worker from publishing an extra case or applying a stale replacement.
4. Use a representative synthetic PDF and CSV against the deployed stack. Verify successful direct upload, a failed parse, a duplicate finalize request, a retry after a worker restart, and authorization failures across teams. Confirm file versioning and the seven-day intake status counts in Team settings.
5. Do not apply an unconditional lifecycle rule to the intake object prefix. Completed reviews can still reference noncurrent object versions. Reconcile orphaned and failed uploads with database references before cleanup.

## Human-review and operations gate

- Invite an analyst and an independent reviewer. Confirm that they can submit, request changes, and sign off from separate sessions. Check that an outsider cannot list, read, alter, preview, or export the case. Revoke one membership and verify that access stops.
- Require an authorized partner to approve the accepted layouts and validate spreadsheet and PDF extraction against a labelled set. The current release accepts bounded text PDFs and a structured CSV fallback. Scanned PDF OCR, broader table extraction, and multi-file case amendments require validated formats and are outside this pilot slice.
- Verify production sign-in, sign-out and recovery with synthetic pilot accounts. Check the Auth0 Post Change Password hook, hosted login protections, and the effective PostgreSQL runtime role. Set a support contact and provide privacy notices before inviting actual customers.
- Monitor `/ready` for schema and database availability, the worker process for restarts, and intake failure or retry events in operational logs. Alert on growing queued jobs and persistent retries. Test rollback to the previous web and worker versions against the staging schema.
- Set a retention policy and pilot support workflow with the customer. Archive preserves evidence and is reversible. A production export/deletion request workflow, legal holds, purge jobs, and tested database-plus-object recovery are open launch gates. Take a database backup and record matching S3 version references, then restore to isolated staging and compare hashes and signed-off packages before accepting real files.
