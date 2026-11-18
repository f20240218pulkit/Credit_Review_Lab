from decimal import Decimal
import unittest

import pymupdf
from fastapi.testclient import TestClient

from credit_review.api import app


class FinancialWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def latest(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["annual"][-1]

    def test_clean_case_has_source_linked_ratios(self):
        latest = self.latest(self.client.get("/cases/CR-101"))
        self.assertEqual(latest["balance"]["gap"], "0")
        coverage = latest["metrics"]["operating_cash_coverage"]
        self.assertEqual(Decimal(coverage["value"]), Decimal("1.1875"))
        self.assertEqual(coverage["refs"], ["2026-09", "2026-10"])

    def test_review_recovers_only_affected_ratio(self):
        before = self.latest(self.client.get("/cases/CR-102"))
        self.assertIsNone(before["metrics"]["ebitda_margin_percent"]["value"])
        self.assertEqual(before["accounts"]["revenue"]["status"], "duplicate")
        after = self.latest(self.client.post("/cases/CR-102/analyse", json={
            "reviews": [{"row_id": "2026-11", "account": None, "reason": "Duplicate verified"}]
        }))
        self.assertEqual(round(Decimal(after["metrics"]["ebitda_margin_percent"]["value"]), 2),
                         Decimal("13.97"))
        self.assertEqual(after["metrics"]["ebitda_margin_percent"]["refs"], ["2026-02", "2026-01"])
        self.assertEqual(after["balance"]["gap"], "250000")
        self.assertEqual(after["metrics"]["operating_cash_coverage"]["reason"], "nonpositive_denominator")

    def test_ambiguous_and_mixed_currency_evidence(self):
        latest = self.latest(self.client.get("/cases/CR-103"))
        self.assertEqual(latest["accounts"]["ebitda"]["status"], "missing")
        self.assertEqual(latest["accounts"]["debt_service"]["status"], "unsupported_source")
        self.assertIsNone(latest["metrics"]["operating_cash_coverage"]["value"])

    def test_review_rejects_unknown_rows_and_blank_reasons(self):
        url = "/cases/CR-102/analyse"
        for row_id, reason in [("unknown", "has a reason"), ("2026-11", "  ")]:
            with self.subTest(row_id=row_id):
                response = self.client.post(url, json={"reviews": [
                    {"row_id": row_id, "account": None, "reason": reason}
                ]})
                self.assertEqual(response.status_code, 422)

    def test_custom_analysis_preserves_source_coordinates(self):
        response = self.client.post("/analyse", json={
            "years": [2025, 2026], "rows": [{
                "id": "line-1", "year": 2026, "label": "Sales revenue", "value": "19.52",
                "unit": "thousands", "currency": "INR", "document": "statement.pdf",
                "line": 1, "page": 2, "bbox": [10, 20, 200, 36],
            }],
        })
        latest = self.latest(response)
        self.assertEqual(Decimal(latest["accounts"]["revenue"]["value"]), Decimal("19520"))
        self.assertEqual(response.json()["rows"][0]["bbox"], [10.0, 20.0, 200.0, 36.0])

    def test_pdf_evidence_round_trip(self):
        pdf = self.client.get("/cases/CR-102/statement.pdf")
        self.assertEqual(pdf.status_code, 200)
        extracted = self.client.post("/extract", content=pdf.content,
                                     headers={"Content-Type": "application/pdf"})
        self.assertEqual(extracted.status_code, 200, extracted.text)
        rows = extracted.json()
        self.assertEqual(len(rows), 21)
        self.assertTrue(all(row["page"] and row["bbox"] for row in rows))
        with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
            for row in rows:
                source_text = document[row["page"] - 1].get_textbox(row["bbox"])
                self.assertIn(row["label"], source_text)
        result = self.client.post("/analyse", json={"years": [2025, 2026], "rows": rows})
        latest = self.latest(result)
        self.assertEqual(latest["accounts"]["revenue"]["status"], "duplicate")
        self.assertEqual(latest["balance"]["gap"], "250000")

        duplicate = next(row for row in rows if row["year"] == 2026 and row["label"] == "Sales revenue")
        reviewed = self.client.post("/analyse", json={
            "years": [2025, 2026], "rows": rows, "reviews": [
                {"row_id": duplicate["id"], "account": None, "reason": "Competing line excluded"}
            ],
        })
        latest = self.latest(reviewed)
        self.assertEqual(round(Decimal(latest["metrics"]["ebitda_margin_percent"]["value"]), 2),
                         Decimal("13.97"))
        self.assertEqual(latest["balance"]["gap"], "250000")

    def test_extract_rejects_invalid_pdf(self):
        response = self.client.post("/extract", content=b"%PDF-invalid",
                                    headers={"Content-Type": "application/pdf"})
        self.assertEqual(response.status_code, 422)
