# Security controls and deployment checks

| # | Control | Coverage |
|---|---|---|
| 1 | HSTS | The app sets a two-year lifetime on HTTPS responses and on Vercel. Vercel also documents a default two-year edge header. |
| 2 | CSRF | Workspace mutations require a session-bound token and reject cross-site browser requests. Sign-out requires a hidden form token. OAuth callbacks use Authlib state and PKCE. |
| 3 | Revoke sessions after password change | The server checks stored sessions on each authenticated workspace request. Connect the Auth0 Action below to revoke all of an affected database user's app sessions. |
| 4 | Reset link expiry | Auth0 issues the link. Verify the Change Password (Link) template expiry and single-use behavior in the tenant. |
| 5 | User enumeration | The app uses generic sign-in failures and has no email lookup or reset endpoint. Verify Auth0's hosted login, reset and signup responses for your enabled flows. |
| 6 | File allowlist | PDF MIME type and signature, parsing, 2 MiB, 10 pages, 500 rows, bounded PDF object count, and representative active-content checks. Originals are served as downloads. |
| 7–8 | Payment webhooks and prices | The app has no payments or pricing. |
| 9–10 | Prompt injection and AI usage | The app uses deterministic label matching and financial calculations. It has no generative model or remote AI calls. |
| 11 | Request size | Actual body bytes are capped at 2 MiB for PDFs, 1 MiB for other API requests and 4 KiB for auth requests. The limits also apply without Content-Length. |
| 12 | Password reset rate limiting | Auth0 controls reset endpoints. Verify the limit for the tenant plan and any reset-specific bot controls available to that plan. |
| 13 | Input and output handling | Type, length and control-character checks guard new workspace text. User-facing HTML treats stored text as data, and database queries bind values. |
| 14 | CORS | Workspace fetches are same-origin. The app supplies no cross-origin allowlist or credentialed CORS responses. |
| 15 | Directory listing | Static routes serve named assets, with directory indexes disabled. |
| 16 | Admin route | The app has no admin interface or default admin route. |
| 17 | Failed-login blocking | Enable and verify Auth0 Brute-Force Protection with Account Lockout if full-account blocking is required. |
| 18 | Security events | The app emits named login, sign-out, session revocation, request rejection and upload events without tokens, subject IDs or document content. Auth0 logs provider-side events. |
| 19 | Cookie flags | The Auth0 app session uses HttpOnly, Secure on HTTPS, and SameSite=Lax for the OAuth redirect. Guest sessions use HttpOnly and SameSite=Strict with Secure on Vercel. Account sessions expire server-side after eight hours. |
| 20 | Database permissions | Manual schema mode supports a DDL-free runtime login. Production must switch to a restricted role using the steps below. |

Application logs are available through the hosting provider. Set retention and alerts in that provider as needed. The PDF checks reduce active-content exposure within the supported statement formats. The app still stores the original uploaded document and uses a native PDF parser.

## Auth0 production configuration

1. Generate a distinct random secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store it as `CREDIT_AUTH0_PASSWORD_HOOK_SECRET` in the Vercel **Production** environment. Redeploy before deploying the Action. In the Auth0 Action, add the same secret under that name and set the Action secret `CREDIT_APP_ORIGIN` to the app's HTTPS production origin. Keep both secrets in their respective secret stores.
2. In Auth0, create and deploy a **Post Change Password** Action for the email/password database connection. Add it to the Post Change Password trigger. Use [auth0-post-change-password.js](auth0-post-change-password.js) as the Action source. If the tenant serves multiple unrelated database connections, add the optional Action secret `CREDIT_AUTH0_CONNECTION` with the connection name used by this app to scope the event.
3. Under **Branding → Email Templates → Change Password (Link)**, target a 30-minute URL lifetime where the template customization is available. Test expiry and single use with a dedicated test account. In a separate browser, change that account's password and check that the previous signed-in browsers require a fresh sign-in.
4. Under **Security → Attack Protection**, enable Brute-Force Protection and turn on **Account Lockout** for full-account blocking. Verify account recovery and enable Suspicious IP Throttling for login abuse. Auth0 treats password-reset requests separately. Review the reset endpoint limits for the tenant plan and enable reset-specific Bot Detection where supported. Compare hosted login and password-reset messages for existing and nonexistent test addresses. If self-signup is enabled, verify that flow separately and document any provider response that discloses registration status.
5. Check Auth0 tenant logs for successful password resets and Action delivery failures, and application logs for `password_change_sessions_revoked`. Auth0 runs this Action asynchronously. The app can revoke its own sessions after delivery. Password changes at external social identity providers have their own policies and do not send this Auth0 database-connection event.

Provider references: [Post Change Password](https://auth0.com/docs/customize/actions/explore-triggers/post-change-password), [reset email configuration](https://support.auth0.com/center/s/article/Configuration-guidance-for-a-password-reset-flow-using-the-Change-Password-Email-Link), [Brute-Force Protection](https://auth0.com/docs/secure/attack-protection/brute-force-protection), and [rate limits](https://auth0.com/docs/troubleshoot/customer-support/operational-policies/rate-limit-policy/rate-limit-configurations).

## PostgreSQL runtime role

1. Use the database owner or deployment role to provision the current schema. From `v2`, with the owner URL already set in the shell environment, run:

   ```sh
   python -c 'import os; from credit_review.workspace import init_postgres; init_postgres(os.environ["CREDIT_DATABASE_URL"])'
   ```

2. Create a separate login named `credit_review_runtime` using your database provider or an administrator. Use an independently generated credential and a role with no administrative memberships, ownership, superuser, role creation or database creation privileges. Grant database CONNECT if your database restricts it. Run the following as the owner in the app database, substituting the correct schema if the deployment uses a dedicated one:

   ```sql
   GRANT USAGE ON SCHEMA public TO credit_review_runtime;
   GRANT SELECT, INSERT, UPDATE ON public.cases TO credit_review_runtime;
   GRANT SELECT, INSERT ON public.documents, public.snapshots TO credit_review_runtime;
   GRANT SELECT, INSERT, DELETE ON public.account_sessions TO credit_review_runtime;
   GRANT USAGE ON SEQUENCE public.cases_rowid_seq TO credit_review_runtime;
   ```

   Verify that this login has no effective schema CREATE privilege, including privileges inherited from PUBLIC or a provider-managed role. If your existing shared schema grants CREATE to PUBLIC, use a dedicated schema or have the administrator plan the privilege change before affecting other applications. Keep the migration owner credential separate from the app deployment.
3. Set the restricted URL in Vercel as `CREDIT_DATABASE_URL`, and set `CREDIT_DB_MANAGED_SCHEMA=1` for the same deployment. Run the normal create, review, replacement, sign-in and logout checks under the restricted login. Reapply schema migrations with the owner before future releases that change the schema.

See [PostgreSQL GRANT](https://www.postgresql.org/docs/current/sql-grant.html).

## Client/API rollout

Account cookies from versions without server-managed sessions require one fresh sign-in. Persisted workspace cases and PDFs remain linked to the same account key. API clients that mutate a workspace must first GET `/workspace`, retain the session cookie, and send the returned `X-CSRF-Token` header on workspace POSTs. Fetch a fresh token after changing accounts.
