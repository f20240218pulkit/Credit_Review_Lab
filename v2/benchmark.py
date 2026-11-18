"""Measure extraction and mapping independently on the synthetic reference set."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

import pymupdf

from credit_review.api import CASES, MAPPER
from credit_review.documents import extract_rows, make_demo_pdf
from credit_review.engine import normalise
from credit_review.web import LAYOUTS


def evaluate():
    benchmark = json.loads(Path(__file__).with_name("tests").joinpath("mapping_challenge.json").read_text())
    mapping = Counter()
    false_matches = []
    for label, expected in benchmark:
        predicted = MAPPER.suggest(label)["account"]
        if expected is None:
            mapping["unknown"] += 1
            mapping["withheld"] += predicted is None
        else:
            mapping["known"] += 1
            mapping["alias_correct"] += MAPPER.exact.get(normalise(label)) == expected
            mapping["model_correct"] += predicted == expected
        if predicted and predicted != expected:
            false_matches.append({"label": label, "expected": expected, "suggested": predicted})

    extraction = {}
    for case_id, case in CASES.items():
        pdf = make_demo_pdf(case, LAYOUTS[case_id])
        rows = extract_rows(pdf)
        expected = Counter((row["year"], normalise(row["label"]), row["currency"], row["value"] * 1000)
                           for row in case["rows"])
        actual = Counter((row.year, normalise(row.label), row.currency,
                          row.value * (1000 if row.unit == "thousands" else 1)) for row in rows)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            evidence_hits = sum(normalise(row.label) in normalise(document[row.page - 1].get_textbox(row.bbox))
                                for row in rows)
        extraction[case_id] = {"expected": sum(expected.values()), "extracted": sum(actual.values()),
                               "matching_amount_period_currency_label": sum((expected & actual).values()),
                               "source_highlights_verified": evidence_hits}
    return {"extraction": extraction, "mapping": dict(mapping), "incorrect_suggestions": false_matches}


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2))
