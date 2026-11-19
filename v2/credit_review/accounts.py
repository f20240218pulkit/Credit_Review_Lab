"""Account sessions backed by Auth0 Universal Login."""
from hashlib import sha256
from hmac import compare_digest
import logging
import os
from secrets import token_urlsafe
from time import time
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .security import audit, valid_csrf
from .workspace import database

NO_STORE = {"Cache-Control": "private, no-store"}


SESSION_SECONDS = 8 * 60 * 60


class PasswordChange(BaseModel):
    subject: str = Field(min_length=1, max_length=255)


def account_owner(request):
    return getattr(request.state, "account", None)


def account_key(domain, subject):
    return "account:" + sha256(f"{domain}\0{subject}".encode()).hexdigest()


def session_digest(session_id):
    return sha256(session_id.encode()).hexdigest()


def active_account(session_id):
    with database() as db:
        row = db.execute("SELECT owner FROM account_sessions WHERE id=? AND expires_at>?",
                         (session_digest(session_id), int(time()))).fetchone()
    return row[0] if row else None


def start_session(owner):
    session_id = token_urlsafe(32)
    now = int(time())
    with database(write=True) as db:
        db.execute("DELETE FROM account_sessions WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO account_sessions (id, owner, expires_at) VALUES (?, ?, ?)",
                   (session_digest(session_id), owner, now + SESSION_SECONDS))
    return session_id


def register_accounts(app):
    domain = os.getenv("AUTH0_DOMAIN", "").strip()
    client_id = os.getenv("AUTH0_CLIENT_ID", "").strip()
    client_secret = os.getenv("AUTH0_CLIENT_SECRET", "")
    secret = os.getenv("CREDIT_SESSION_SECRET", "")
    origin = os.getenv("CREDIT_APP_ORIGIN", "").rstrip("/")
    email_connection = os.getenv("AUTH0_EMAIL_CONNECTION", "Username-Password-Authentication")
    hook_secret = os.getenv("CREDIT_AUTH0_PASSWORD_HOOK_SECRET", "")
    if hook_secret and (len(hook_secret) < 32 or not hook_secret.isascii() or hook_secret == secret):
        raise RuntimeError("Use a distinct password-change hook secret of at least 32 ASCII characters")
    oauth = None
    params = (domain, client_id, client_secret, secret, origin)
    if all(params):
        endpoint = urlsplit("https://" + domain)
        app_url = urlsplit(origin)
        local = app_url.scheme == "http" and app_url.hostname in {"localhost", "127.0.0.1"}
        if (not endpoint.hostname or endpoint.netloc != domain or endpoint.path or endpoint.query
                or endpoint.fragment or endpoint.username or endpoint.port
                or not app_url.hostname or app_url.username or app_url.path or app_url.query or app_url.fragment
                or (app_url.scheme != "https" and not local) or len(secret) < 32):
            raise RuntimeError("Invalid Auth0 domain, app origin, or session secret")
        from authlib.integrations.starlette_client import OAuth
        from starlette.middleware.sessions import SessionMiddleware

        @app.middleware("http")
        async def resolve_account(request, call_next):
            path = request.url.path
            if path in {"/", "/signin", "/settings", "/auth/logout"} or path.startswith("/workspace"):
                session_id = request.session.get("session_id")
                if isinstance(session_id, str):
                    request.state.account = await run_in_threadpool(active_account, session_id)
                    if not request.state.account:
                        request.session.clear()
                        audit("inactive_account_session")
                elif "account" in request.session:
                    # Remove legacy signed sessions that could not be revoked server-side.
                    request.session.clear()
            return await call_next(request)

        app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="credit_account",
                           same_site="lax", https_only=not local, max_age=SESSION_SECONDS)
        registry = OAuth()
        oauth = registry.register(
            "auth0", client_id=client_id, client_secret=client_secret,
            server_metadata_url=f"https://{domain}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256",
                           "timeout": 10.0},
        )

    @app.get("/signin", response_class=HTMLResponse, include_in_schema=False)
    def signin(request: Request, error: bool = False):
        if account_owner(request):
            return RedirectResponse("/", status_code=303, headers=NO_STORE)
        if oauth is None:
            controls = "<p>Account sign-in has not been configured on this deployment.</p>"
        else:
            controls = (
                '<div class="controls"><a class="signin-option" href="/auth/login/email">Continue with email</a>'
                '<a class="signin-option" href="/auth/login/google">Continue with Google</a></div>'
            )
        feedback = '<p role="alert">Sign-in could not be completed. Please try again.</p>' if error else ""
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Sign in | Credit Review Lab</title><link rel="stylesheet" href="/assets/style.css"></head>'
            '<body><header><div class="brand">Credit Review Lab</div><a href="/">Back to workspace</a></header>'
            '<main><section class="intro"><h1>Sign in</h1>'
            '<p>Sign in to upload statement packs and return to your saved reviews.</p>'
            f'{feedback}{controls}<p><a href="/">Explore public demos</a></p></section></main></body></html>',
            headers=NO_STORE,
        )

    @app.get("/auth/login/{method}", include_in_schema=False)
    async def login(request: Request, method: str):
        if oauth is None:
            raise HTTPException(503, "Account sign-in is not configured")
        connections = {"email": email_connection, "google": "google-oauth2"}
        if method not in connections:
            raise HTTPException(404, "Sign-in method not available")
        from authlib.integrations.base_client import OAuthError
        from httpx import HTTPError
        try:
            response = await oauth.authorize_redirect(request, origin + "/auth/callback",
                                                       connection=connections[method], prompt="login")
        except (OAuthError, HTTPError):
            audit("auth_login_redirect_failed", logging.WARNING)
            return RedirectResponse("/signin?error=true", status_code=303, headers=NO_STORE)
        response.headers.update(NO_STORE)
        return response

    @app.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request):
        if oauth is None:
            raise HTTPException(503, "Account sign-in is not configured")
        from authlib.integrations.base_client import OAuthError
        from authlib.jose.errors import JoseError
        from httpx import HTTPError
        from joserfc.errors import JoseError as RFCJoseError
        try:
            token = await oauth.authorize_access_token(request)
        except (OAuthError, HTTPError, JoseError, RFCJoseError):
            audit("auth_callback_rejected", logging.WARNING)
            return RedirectResponse("/signin?error=true", status_code=303, headers=NO_STORE)
        subject = (token.get("userinfo") or {}).get("sub")
        if not token.get("id_token") or not isinstance(subject, str) or not subject:
            audit("auth_identity_rejected", logging.WARNING)
            return RedirectResponse("/signin?error=true", status_code=303, headers=NO_STORE)
        request.session.clear()
        request.session["session_id"] = await run_in_threadpool(start_session, account_key(domain, subject))
        audit("auth_login_succeeded")
        return RedirectResponse("/", status_code=303, headers=NO_STORE)

    @app.post("/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        if oauth is None or not account_owner(request):
            return RedirectResponse("/", status_code=303, headers=NO_STORE)
        session_id = request.session["session_id"]
        data = (await request.body()).decode("ascii", errors="replace")
        supplied = parse_qs(data, max_num_fields=8).get("csrf", []) if data.count("&") < 8 else []
        if (request.headers.get("content-type", "").partition(";")[0] != "application/x-www-form-urlencoded"
                or len(supplied) != 1 or not valid_csrf(request, session_id, supplied[0])):
            audit("logout_csrf_rejected", logging.WARNING)
            raise HTTPException(403, "Request verification failed. Reload the workspace and try again.")

        def revoke_session():
            with database(write=True) as db:
                db.execute("DELETE FROM account_sessions WHERE id=?", (session_digest(session_id),))

        await run_in_threadpool(revoke_session)
        request.session.clear()
        audit("auth_logout_succeeded")
        query = urlencode({"client_id": client_id, "returnTo": origin + "/"})
        return RedirectResponse(f"https://{domain}/v2/logout?{query}", status_code=303, headers=NO_STORE)

    @app.post("/auth/password-changed", status_code=204, include_in_schema=False)
    def password_changed(body: PasswordChange, request: Request):
        if oauth is None or not hook_secret:
            raise HTTPException(404, "Not found")
        authorization = request.headers.get("authorization", "")
        if not authorization.isascii() or not compare_digest(authorization, "Bearer " + hook_secret):
            audit("password_change_hook_rejected", logging.WARNING)
            raise HTTPException(403, "Request verification failed")
        with database(write=True) as db:
            db.execute("DELETE FROM account_sessions WHERE owner=?", (account_key(domain, body.subject),))
        audit("password_change_sessions_revoked")

    return oauth is not None
