from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pymupdf

from credit_review.api import CASES, app
from credit_review.documents import make_demo_pdf
from credit_review.security import JSON_LIMIT


class SecurityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"CREDIT_DB": directory + "/security.sqlite3"}))
        self.client = TestClient(app, base_url="https://testserver")

    def test_session_bound_tokens_and_security_headers(self):
        page = self.client.get("/")
        self.assertIn("max-age=", page.headers["strict-transport-security"])
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/assets/").status_code, 404)
        self.assertEqual(self.client.get("/admin").status_code, 404)
        self.assertNotIn("access-control-allow-origin", self.client.get(
            "/workspace", headers={"Origin": "https://other.example"}).headers)
        bootstrap = self.client.get("/workspace")
        self.assertEqual(bootstrap.headers["cache-control"], "private, no-store")
        token = bootstrap.headers["X-CSRF-Token"]
        other = TestClient(app, base_url="https://testserver")
        wrong = other.get("/workspace").headers["X-CSRF-Token"]
        route = "/workspace/demo/CR-101"
        for headers in ({}, {"X-CSRF-Token": wrong},
                        {"X-CSRF-Token": token, "Origin": "https://other.example"}):
            self.assertEqual(self.client.post(route, headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/workspace").json(), [])
        self.assertEqual(self.client.post(route, headers={"X-CSRF-Token": token}).status_code, 201)

    def test_request_limits_with_and_without_content_length(self):
        self.assertEqual(self.client.post("/analyse", content=b"x" * (JSON_LIMIT + 1)).status_code, 413)
        body = iter([b"x" * (JSON_LIMIT // 2), b"x" * (JSON_LIMIT // 2 + 1)])
        self.assertEqual(self.client.post("/analyse", content=body).status_code, 413)
        self.assertEqual(self.client.post("/analyse", content=b"{}", headers={
            "Content-Encoding": "gzip"}).status_code, 415)

    def test_upload_type_and_attachments(self):
        token = self.client.get("/workspace").headers["X-CSRF-Token"]
        headers = {"X-CSRF-Token": token, "Content-Type": "application/pdf"}
        route = "/workspace/upload?name=Example"
        self.assertEqual(self.client.post(route, content=b"Plain text", headers=headers).status_code, 422)
        pdf = make_demo_pdf(CASES["CR-101"])
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            document.embfile_add("example.txt", b"Sample note")
            attachment_pdf = document.tobytes()
        self.assertEqual(self.client.post(route, content=attachment_pdf, headers=headers).status_code, 422)
        blocked = self.client.post("/workspace/upload", params={"name": "Line\x00break"}, content=pdf, headers=headers)
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(self.client.get("/workspace").json(), [])
        self.assertEqual(self.client.post(route, content=pdf, headers=headers).status_code, 201)
