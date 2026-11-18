from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

from credit_review.api import CASES, app
from credit_review.documents import make_demo_pdf


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"CREDIT_DB": tmp + "/test.sqlite3"}))
        self.client = TestClient(app)

    def create(self, fixture="CR-102"):
        response = self.client.post("/workspace/demo/" + fixture)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def review(self, state, label, account):
        row = next(item for item in state["analysis"]["rows"]
                   if item["year"] == 2026 and item["label"] == label)
        return self.client.post(f'/workspace/{state["id"]}/reviews', json={
            "revision": state["revision"], "row_id": row["id"], "account": account,
            "reason": "Source statement verified",
        })

    def test_three_demo_layouts(self):
        for case, rows in [("CR-101", 20), ("CR-102", 21), ("CR-103", 19)]:
            with self.subTest(case=case):
                state = self.create(case)
                self.assertEqual(len(state["analysis"]["rows"]), rows)
                self.assertEqual(len(state["versions"]), 1)

    def test_review_snapshot_replacement_and_restart(self):
        initial = self.create()
        result = self.review(initial, "Sales revenue", None)
        self.assertEqual(result.status_code, 200, result.text)
        reviewed = result.json()
        result = self.review(reviewed, "Cash generated from operations", "operating_cash")
        self.assertEqual(result.status_code, 200, result.text)
        reviewed = result.json()
        root = f'/workspace/{reviewed["id"]}'
        after_reload = TestClient(app, cookies=self.client.cookies)
        self.assertEqual(after_reload.get(root).json(), reviewed)

        response = self.client.post(root + "/snapshots", json={
            "revision": reviewed["revision"], "decline_percent": 15, "notes": "Meridian credit review",
        })
        self.assertEqual(response.status_code, 201, response.text)
        snapshot = response.json()
        self.assertEqual(snapshot["status"], "draft")
        export = root + "/snapshots/" + snapshot["id"]
        prior = self.client.get(export + ".json").content
        memo = self.client.get(export + ".pdf")
        with pymupdf.open(stream=memo.content, filetype="pdf") as document:
            self.assertIn("Meridian credit review", "".join(page.get_text() for page in document))

        replacement = self.client.post(root + "/replacement",
                                       params={"demo": True, "revision": reviewed["revision"]})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        state = replacement.json()
        self.assertEqual(state["comparison"]["reopened_reviews"], 1)
        self.assertEqual(len(state["reviews"]), 1)
        latest = state["analysis"]["annual"][-1]
        self.assertEqual(round(Decimal(latest["metrics"]["ebitda_margin_percent"]["value"]), 2),
                         Decimal("13.97"))
        self.assertEqual(Decimal(latest["metrics"]["operating_cash_coverage"]["value"]), Decimal(".95"))
        self.assertEqual(latest["balance"]["gap"], "250000")
        self.assertEqual(self.client.get(export + ".json").content, prior)
        self.assertEqual(self.client.get(root + "/scenario?decline=15").json()["stressed"], "0.8075")
        self.assertEqual(self.review(initial, "Sales revenue", None).status_code, 409)

        old = initial["analysis"]["rows"][-1]
        preview = self.client.get(root + f'/documents/{old["document"]}/page/{old["page"]}',
                                  params={"highlight": old["id"]})
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.content.startswith(b"\x89PNG"))
        self.assertEqual(TestClient(app).get(export + ".json").status_code, 404)
        self.assertEqual(TestClient(app).get(root).status_code, 404)
        self.assertEqual(TestClient(app).get(root + "/source/" + old["document"]).status_code, 404)

    def test_upload_and_identical_retry(self):
        pdf = make_demo_pdf(CASES["CR-101"])
        response = self.client.post("/workspace/upload?name=Example", content=pdf,
                                    headers={"Content-Type": "application/pdf"})
        self.assertEqual(response.status_code, 201, response.text)
        original = response.json()
        self.assertNotEqual(original["id"], "CR-101")
        reused = self.client.post(f'/workspace/{original["id"]}/replacement?revision=1',
                                  content=pdf, headers={"Content-Type": "application/pdf"})
        self.assertEqual(reused.json(), original)
        with patch.dict("os.environ", {"CREDIT_PUBLIC_DEMO": "1"}):
            blocked = self.client.post("/workspace/upload?name=Example", content=pdf,
                                       headers={"Content-Type": "application/pdf"})
            self.assertEqual(blocked.status_code, 403)
            self.assertFalse(self.client.get("/settings").json()["uploads"])

    def test_frontend_assets(self):
        self.assertIn("Review the numbers", self.client.get("/").text)
        self.assertIn("showEvidence", self.client.get("/assets/workbench.js").text)
