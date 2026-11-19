import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from credit_review.api import CASES, MAPPER, parse_pdf, read_pdf
from credit_review.documents import make_demo_pdf
from credit_review.web import register_workspace


class AccountTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "AUTH0_DOMAIN": "tenant.example.com", "AUTH0_CLIENT_ID": "test-client",
            "AUTH0_CLIENT_SECRET": "test-only", "CREDIT_SESSION_SECRET": "test-secret-" * 4,
            "CREDIT_APP_ORIGIN": "https://testserver", "CREDIT_PUBLIC_DEMO": "1",
            "CREDIT_DB": tmp + "/accounts.sqlite3",
            "CREDIT_AUTH0_PASSWORD_HOOK_SECRET": "password-hook-test-key-" * 2,
        }))
        self.provider = self.enterContext(patch("authlib.integrations.starlette_client.OAuth.register"))
        self.remote = self.provider.return_value
        self.remote.authorize_redirect = AsyncMock()
        self.remote.authorize_access_token = AsyncMock()
        app = FastAPI()
        register_workspace(app, CASES, MAPPER, read_pdf, parse_pdf)
        self.client = TestClient(app, base_url="https://testserver")
        self.refresh_csrf(self.client)

    def refresh_csrf(self, client):
        client.headers["X-CSRF-Token"] = client.get("/workspace").headers["X-CSRF-Token"]

    def sign_in(self, client, subject):
        self.remote.authorize_access_token.return_value = {"id_token": "verified-by-provider-client",
                                                           "userinfo": {"sub": subject}}
        response = client.get("/auth/callback", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/")
        self.assertIn("secure", response.headers["set-cookie"].lower())
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", response.headers["set-cookie"].lower())
        self.refresh_csrf(client)

    def test_both_login_buttons_route_to_correct_connections(self):
        from fastapi.responses import RedirectResponse
        self.remote.authorize_redirect.return_value = RedirectResponse("https://tenant.example.com/authorize")
        body = self.client.get("/signin").text
        self.assertIn("Continue with email", body)
        self.assertIn("Continue with Google", body)
        for method, connection in [("email", "Username-Password-Authentication"), ("google", "google-oauth2")]:
            response = self.client.get(f"/auth/login/{method}", follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            positional, options = self.remote.authorize_redirect.call_args
            self.assertEqual(positional[1], "https://testserver/auth/callback")
            self.assertEqual(options["connection"], connection)

    def test_private_workspace_and_sign_out(self):
        root = "/workspace/upload?name=Test"
        pdf = make_demo_pdf(CASES["CR-101"])
        kwargs = {"content": pdf, "headers": {"Content-Type": "application/pdf"}}
        self.assertEqual(self.client.post(root, **kwargs).status_code, 403)
        self.assertEqual(self.client.post("/workspace/demo/CR-101").status_code, 201)
        self.sign_in(self.client, "auth0|first")
        self.assertEqual(self.client.get("/settings").json(), {"uploads": True})
        self.assertIn("Sign out", self.client.get("/").text)
        self.assertEqual(self.client.get("/workspace").json(), [])
        uploaded = self.client.post(root, **kwargs)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        path = "/workspace/" + uploaded.json()["id"]
        self.assertEqual(self.client.get(path).status_code, 200)

        other = TestClient(self.client.app, base_url="https://testserver")
        self.sign_in(other, "google-oauth2|other")
        self.assertEqual(other.get(path).status_code, 404)
        self.assertEqual(other.get(path + "/source/" + uploaded.json()["document"]).status_code, 404)
        self.assertEqual(other.get("/workspace").json(), [])
        same_account = TestClient(self.client.app, base_url="https://testserver")
        self.sign_in(same_account, "auth0|first")
        self.assertEqual(same_account.get(path).status_code, 200)

        saved_cookie = self.client.cookies.get("credit_account")
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get("/").text)[1]
        self.assertEqual(self.client.post("/auth/logout").status_code, 403)
        self.assertEqual(self.client.post("/auth/logout", data={"csrf": csrf},
                                         headers={"Origin": "https://elsewhere.example"}).status_code, 403)
        self.assertEqual(self.client.post("/auth/logout", data={"csrf": csrf}, follow_redirects=False).status_code, 303)
        replay = TestClient(self.client.app, base_url="https://testserver", cookies={"credit_account": saved_cookie})
        self.assertEqual(replay.get(path).status_code, 404)
        self.assertEqual(same_account.get(path).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(root, **kwargs).status_code, 403)

    def test_password_change_revokes_all_matching_sessions(self):
        other_browser = TestClient(self.client.app, base_url="https://testserver")
        unrelated = TestClient(self.client.app, base_url="https://testserver")
        self.sign_in(self.client, "auth0|first")
        self.sign_in(other_browser, "auth0|first")
        self.sign_in(unrelated, "auth0|different")
        path = "/auth/password-changed"
        event = {"subject": "auth0|first"}
        self.assertEqual(self.client.post(path, json=event).status_code, 403)
        self.assertTrue(self.client.get("/settings").json()["uploads"])
        headers = {"Authorization": "Bearer " + "password-hook-test-key-" * 2}
        self.assertEqual(self.client.post(path, json=event, headers=headers).status_code, 204)
        self.assertFalse(self.client.get("/settings").json()["uploads"])
        self.assertFalse(other_browser.get("/settings").json()["uploads"])
        self.assertTrue(unrelated.get("/settings").json()["uploads"])

    def test_session_has_absolute_expiry(self):
        self.sign_in(self.client, "auth0|first")
        with patch("credit_review.accounts.time", return_value=4_000_000_000):
            self.assertFalse(self.client.get("/settings").json()["uploads"])

    def test_callback_requires_id_token_and_subject(self):
        self.remote.authorize_access_token.return_value = {"userinfo": {"sub": "unverified"}}
        response = self.client.get("/auth/callback", follow_redirects=False)
        self.assertEqual(response.headers["Location"], "/signin?error=true")
        self.assertFalse(self.client.get("/settings").json()["uploads"])
