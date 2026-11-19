"""Browser request protection and bounds for request bodies."""
from hashlib import sha256
from hmac import compare_digest
import logging
import os
import re
import unicodedata

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from .documents import MAX_BYTES

CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
JSON_LIMIT = 1024 * 1024
AUTH_LIMIT = 4096
logger = logging.getLogger("uvicorn.error.credit_security")


def audit(event, level=logging.INFO):
    # Only call with fixed event names. Do not include cookies, subject IDs or uploaded data.
    logger.log(level, "security_event=%s", event)


def clean_text(value):
    if any((unicodedata.category(char) == "Cc" and char not in "\t\n\r")
           or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for char in value):
        raise ValueError("Text contains unsupported control characters")
    return value


def csrf_token(session_key):
    return sha256(f"credit-csrf-v1\0{session_key}".encode()).hexdigest()


def valid_csrf(request, session_key, supplied):
    origin = request.headers.get("origin")
    app_origin = os.getenv("CREDIT_APP_ORIGIN", "").rstrip("/")
    allowed = {str(request.base_url).rstrip("/")}
    if app_origin:
        allowed.add(app_origin)
    return bool(
        session_key and supplied and len(supplied) == 64 and supplied.isascii()
        and request.headers.get("sec-fetch-site") != "cross-site"
        and (origin is None or origin in allowed)
        and compare_digest(supplied, csrf_token(session_key))
    )


class RequestSecurity:
    def __init__(self, app):
        self.app = app
        auth_domain = os.getenv("AUTH0_DOMAIN", "").strip()
        self.form_targets = "'self'"
        if re.fullmatch(r"[a-zA-Z0-9.-]+", auth_domain):
            self.form_targets += f" https://{auth_domain}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                policy = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
                storage_origin = os.getenv("CREDIT_UPLOAD_ORIGIN", "").rstrip("/")
                connect = "'self'"
                if re.fullmatch(r"https://[a-zA-Z0-9.-]+", storage_origin):
                    connect += " " + storage_origin
                if path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
                    policy += ("; default-src 'none'; script-src 'self'; style-src 'self';"
                               f" img-src 'self'; connect-src {connect}; form-action {self.form_targets}")
                headers["Content-Security-Policy"] = policy
                if scope["scheme"] == "https" or os.environ.get("VERCEL") == "1":
                    headers["Strict-Transport-Security"] = "max-age=63072000"
                if path.startswith(("/workspace", "/auth/")):
                    headers["Cache-Control"] = "private, no-store"
            await send(message)

        async def reject(detail, status):
            audit("request_body_rejected", logging.WARNING)
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send_with_headers)

        if scope["method"] not in UNSAFE_METHODS:
            return await self.app(scope, receive, send_with_headers)
        headers = Headers(scope=scope)
        if headers.get("content-encoding", "identity").lower() != "identity":
            return await reject("Compressed request bodies are unsupported", 415)
        media_type = headers.get("content-type", "").partition(";")[0].strip().lower()
        limit = AUTH_LIMIT if path.startswith("/auth/") else (MAX_BYTES if media_type == "application/pdf" else JSON_LIMIT)
        length = headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 12:
                return await reject("Invalid Content-Length", 400)
            if int(length) > limit:
                return await reject("Request body exceeds the size limit", 413)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await reject("Request body exceeds the size limit", 413)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if length is not None and int(length) != len(body):
            return await reject("Content-Length does not match the request body", 400)
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send_with_headers)
