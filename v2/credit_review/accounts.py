"""Account sessions backed by Auth0 Universal Login."""
from hashlib import sha256
import os
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

NO_STORE = {"Cache-Control": "private, no-store"}


def account_owner(request):
    """Return the account key only from Starlette's signed session cookie."""
    return request.scope.get("session", {}).get("account")


def register_accounts(app):
    domain = os.getenv("AUTH0_DOMAIN", "").strip()
    client_id = os.getenv("AUTH0_CLIENT_ID", "").strip()
    client_secret = os.getenv("AUTH0_CLIENT_SECRET", "")
    secret = os.getenv("CREDIT_SESSION_SECRET", "")
    origin = os.getenv("CREDIT_APP_ORIGIN", "").rstrip("/")
    email_connection = os.getenv("AUTH0_EMAIL_CONNECTION", "Username-Password-Authentication")
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

        app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="credit_account",
                           same_site="lax", https_only=not local, max_age=8 * 60 * 60)
        registry = OAuth()
        oauth = registry.register(
            "auth0", client_id=client_id, client_secret=client_secret,
            server_metadata_url=f"https://{domain}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256",
                           "timeout": 10.0},
        )

        @app.middleware("http")
        async def check_origin(request, call_next):
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                source = request.headers.get("origin")
                if source is not None and source != origin:
                    return HTMLResponse("Request origin does not match the workspace.", status_code=403)
            return await call_next(request)

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
            return RedirectResponse("/signin?error=true", status_code=303, headers=NO_STORE)
        subject = token.get("userinfo", {}).get("sub")
        if not token.get("id_token") or not isinstance(subject, str) or not subject:
            return RedirectResponse("/signin?error=true", status_code=303, headers=NO_STORE)
        request.session.clear()
        request.session["account"] = "account:" + sha256(f"{domain}\0{subject}".encode()).hexdigest()
        return RedirectResponse("/", status_code=303, headers=NO_STORE)

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request):
        if oauth is None or not account_owner(request):
            return RedirectResponse("/", status_code=303, headers=NO_STORE)
        request.session.clear()
        query = urlencode({"client_id": client_id, "returnTo": origin + "/"})
        return RedirectResponse(f"https://{domain}/v2/logout?{query}", status_code=303, headers=NO_STORE)

    return oauth is not None
