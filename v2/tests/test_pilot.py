from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from credit_review.accounts import email_digest
from credit_review.api import CASES, MAPPER, parse_pdf, read_pdf
from credit_review.documents import make_demo_pdf
from credit_review.intake_jobs import run_one
from credit_review.web import register_workspace


class PilotTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "CREDIT_DB": root + "/pilot.sqlite", "CREDIT_PUBLIC_DEMO": "1",
            "CREDIT_PILOT_INVITE_ONLY": "1", "CREDIT_PILOT_ADMINS": "admin@example.test",
        }))
        self.app = FastAPI()
        register_workspace(self.app, CASES, MAPPER, read_pdf, parse_pdf)

        @self.app.middleware("http")
        async def test_identity(request, call_next):
            email = request.headers.get("x-test-user")
            if email:
                request.state.account = "account:" + email_digest(email)
                request.scope["session"] = {"session_id": email_digest("session:" + email),
                                            "verified_email": email_digest(email)}
            return await call_next(request)

        self.admin = self.client("admin@example.test")
        self.analyst = self.client("analyst@example.test")
        self.reviewer = self.client("reviewer@example.test")
        self.outsider = self.client("outsider@example.test")
        self.assertEqual(self.outsider.post("/workspace/organizations", json={"name": "No"}).status_code, 403)
        result = self.admin.post("/workspace/organizations", json={"name": "Pilot team"})
        self.assertEqual(result.status_code, 201, result.text)
        self.org = result.json()["id"]
        self.analyst_id = "account:" + email_digest("analyst@example.test")
        self.reviewer_id = "account:" + email_digest("reviewer@example.test")
        for user, email, role, name in [
            (self.analyst, "analyst@example.test", "analyst", "Analyst"),
            (self.reviewer, "reviewer@example.test", "reviewer", "Reviewer"),
        ]:
            code = self.admin.post(f"/workspace/organizations/{self.org}/invitations",
                                   json={"email": email, "role": role, "name": name})
            self.assertEqual(code.status_code, 201, code.text)
            token = code.json()["path"].split("=")[1]
            self.assertEqual(self.outsider.post("/workspace/invitations/accept", json={"token": token}).status_code, 404)
            self.assertEqual(user.post("/workspace/invitations/accept", json={"token": token}).status_code, 200)
            self.assertEqual(user.post("/workspace/invitations/accept", json={"token": token}).status_code, 404)

    def client(self, email):
        client = TestClient(self.app, headers={"x-test-user": email})
        response = client.get("/workspace")
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.headers["X-CSRF-Token"]
        return client

    def test_upload_review_signoff_and_archive(self):
        data = ("year,label,value,unit,currency\n" + "\n".join(
            f'{r["year"]},{r["label"]},{r["value"]},thousands,INR' for r in CASES["CR-101"]["rows"]
        )).encode()
        params = {"name": "Aster Components", "organization": self.org}
        headers = {"Content-Type": "text/csv"}
        path = "/workspace/upload"
        self.assertEqual(self.reviewer.post(path, params=params, content=data, headers=headers).status_code, 403)
        self.assertEqual(self.outsider.post(path, params=params, content=data, headers=headers).status_code, 404)
        response = self.analyst.post(path, params=params, content=data, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        state = response.json()
        base = "/workspace/" + state["id"]
        original = base + "/original/" + state["document"]
        self.assertEqual(self.reviewer.get(original).content, data)
        self.assertEqual(self.outsider.get(original).status_code, 404)
        self.assertEqual(self.outsider.get(base).status_code, 404)
        self.assertEqual(self.analyst.post(base + "/workflow", json={
            "action": "submit", "revision": state["revision"], "reviewer": self.reviewer_id,
        }).status_code, 409)
        for row in state["analysis"]["rows"]:
            result = self.analyst.post(base + "/reviews", json={
                "revision": state["revision"], "row_id": row["id"], "account": row["account"], "reason": "Verified source",
            })
            self.assertEqual(result.status_code, 200, result.text)
            state = result.json()
        result = self.analyst.post(base + "/workflow", json={
            "action": "submit", "revision": state["revision"], "reviewer": self.reviewer_id, "notes": "Reviewed pack",
        })
        self.assertEqual(result.status_code, 200, result.text)
        state = result.json()
        action = {"action": "sign_off", "revision": state["revision"]}
        self.assertEqual(self.admin.post(base + "/workflow", json=action).status_code, 403)
        change = self.reviewer.post(base + "/workflow", json={**action, "action": "request_changes", "notes": "Verify cash"})
        self.assertEqual(change.json()["workflow"]["status"], "changes_requested")
        submitted = self.analyst.post(base + "/workflow", json={
            "action": "submit", "revision": change.json()["revision"], "reviewer": self.reviewer_id,
        })
        action["revision"] = submitted.json()["revision"]
        signed = self.reviewer.post(base + "/workflow", json=action)
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(signed.json()["workflow"]["status"], "signed_off")
        snapshot = base + "/snapshots/" + signed.json()["snapshots"][-1]["id"]
        self.assertEqual(self.outsider.get(snapshot + ".pdf").status_code, 404)
        evidence = self.analyst.get(snapshot + ".json").json()
        self.assertEqual(evidence["sign_off"]["reviewer"], self.reviewer_id)
        self.assertEqual(evidence["statement_versions"][0]["original_sha256"], sha256(data).hexdigest())
        self.assertTrue(self.analyst.get(snapshot + ".pdf").content.startswith(b"%PDF"))
        archived = self.analyst.post(base + "/archive", json={"revision": signed.json()["revision"], "archived": True})
        self.assertTrue(archived.json()["archived"])
        self.assertEqual(self.analyst.get(snapshot + ".json").json(), evidence)
        restored = self.analyst.post(base + "/archive", json={"revision": archived.json()["revision"], "archived": False})
        replacement = self.analyst.post(base + "/replacement", params={"revision": restored.json()["revision"]},
                                        content=make_demo_pdf(CASES["CR-101"]),
                                        headers={"Content-Type": "application/pdf"})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertEqual(replacement.json()["workflow"]["status"], "draft")
        self.assertEqual(self.analyst.get(snapshot + ".json").json(), evidence)

    def test_private_upload_version_pin_and_idempotency(self):
        payload = make_demo_pdf(CASES["CR-101"])
        checksum = sha256(payload).hexdigest()
        body = {"name": "Queued pack", "organization": self.org, "size": len(payload),
                "media_type": "application/pdf", "checksum": checksum}
        params = {"VersionId": "pinned", "ContentLength": len(payload), "ContentType": "application/pdf",
                  "Metadata": {"sha256": checksum}}
        with patch("credit_review.object_storage.enabled", return_value=True), \
                patch("credit_review.object_storage.authorize", return_value={"url": "https://storage.test", "fields": {}}), \
                patch("credit_review.object_storage.describe", return_value=params), \
                patch("credit_review.object_storage.read_version", return_value=payload) as read:
            intent = self.analyst.post("/workspace/intake", json=body)
            self.assertEqual(intent.status_code, 201, intent.text)
            job = "/workspace/intake/" + intent.json()["id"]
            self.assertEqual(self.outsider.get(job).status_code, 404)
            self.assertEqual(self.analyst.post(job + "/finalize").json()["status"], "queued")
            self.assertEqual(self.analyst.post(job + "/finalize").json()["status"], "queued")
            self.assertTrue(run_one(parse_pdf, MAPPER))
            self.assertFalse(run_one(parse_pdf, MAPPER))
            status = self.analyst.get(job).json()
            self.assertEqual(status["status"], "complete")
            self.assertEqual(read.call_args.args[1], "pinned")
            path = "/workspace/" + status["case_id"]
            case = self.reviewer.get(path).json()
            self.assertEqual(self.reviewer.get(path + "/source/" + case["document"]).content, payload)
