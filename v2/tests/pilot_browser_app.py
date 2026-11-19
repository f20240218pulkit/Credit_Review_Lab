"""Loopback-only synthetic identities for the pilot browser test."""
import os

from credit_review.accounts import email_digest
from credit_review.api import app

if os.getenv("CREDIT_PILOT_TEST_AUTH") != "1":
    raise RuntimeError("This app is only for the loopback browser test")


@app.middleware("http")
async def test_identity(request, call_next):
    user = request.headers.get("x-pilot-test-user")
    if user in {"owner", "analyst", "reviewer", "outsider"}:
        email = user + "@example.test"
        request.state.account = "account:" + email_digest(email)
        request.scope["session"] = {"session_id": email_digest("session:" + email),
                                    "verified_email": email_digest(email)}
    return await call_next(request)
