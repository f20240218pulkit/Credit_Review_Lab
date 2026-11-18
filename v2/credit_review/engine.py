from collections import Counter
from decimal import Decimal
from math import sqrt
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

Account = Literal[
    "revenue", "ebitda", "current_assets", "current_liabilities", "assets",
    "liabilities", "equity", "debt", "operating_cash", "debt_service",
]
ACCOUNTS = Account.__args__
FORMULAS = {
    "current_ratio": ("current_assets", "current_liabilities", 1),
    "debt_to_ebitda": ("debt", "ebitda", 1),
    "operating_cash_coverage": ("operating_cash", "debt_service", 1),
    "ebitda_margin_percent": ("ebitda", "revenue", 100),
}


class SourceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    year: int = Field(ge=1900, le=2200)
    label: str = Field(min_length=1, max_length=300)
    value: Decimal = Field(max_digits=20, decimal_places=4, allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=30)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    document: str = Field(min_length=1, max_length=200)
    line: int = Field(ge=1)
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None


class Review(BaseModel):
    row_id: str
    account: Account | None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


def normalise(label):
    return re.sub(r"[^a-z0-9]+", " ", label.lower().replace("’", "").replace("'", "")).strip()


def vector(label):
    text = f" {normalise(label)} "
    counts = Counter(text[i:i + 3] for i in range(len(text) - 2))
    norm = sqrt(sum(count * count for count in counts.values()))
    return {gram: count / norm for gram, count in counts.items()} if norm else {}


class Mapper:
    def __init__(self, aliases):
        self.examples = {account: [vector(label) for label in labels] for account, labels in aliases.items()}
        self.exact = {normalise(label): account for account, labels in aliases.items() for label in labels}

    def suggest(self, label):
        match = self.exact.get(normalise(label))
        if match:
            return {"account": match, "method": "exact", "similarity": 1.0}
        query = vector(label)
        scores = sorted(
            (
                (max(sum(weight * sample.get(gram, 0) for gram, weight in query.items())
                     for sample in samples), account)
                for account, samples in self.examples.items()
            ),
            reverse=True,
        )
        (score, account), (runner_up, _) = scores[:2]
        matched = score >= 0.46 and score - runner_up >= 0.09
        return {"account": account if matched else None,
                "method": "similarity" if matched else "abstain", "similarity": round(score, 4)}


def analyse(rows, reviews, mapper, years):
    decisions = {review.row_id: review for review in reviews}
    mapped = []
    for row in rows:
        suggestion = mapper.suggest(row.label)
        review = decisions.get(row.id)
        account = review.account if review else suggestion["account"]
        multiplier = {"units": 1, "thousands": 1000}.get(row.unit)
        amount = row.value * multiplier if row.currency == "INR" and multiplier else None
        mapped.append({**row.model_dump(), "account": account, "suggestion": suggestion,
                       "review": review.model_dump() if review else None, "amount_inr": amount})

    annual = []
    exceptions = []
    for year in sorted(years):
        period_rows = [row for row in mapped if row["year"] == year]
        for row in period_rows:
            if row["review"] is None and row["account"] is None:
                exceptions.append({"year": year, "code": "unmapped_row", "refs": [row["id"]]})
            if row["amount_inr"] is None:
                exceptions.append({"year": year, "code": "unsupported_source", "refs": [row["id"]]})

        accounts = {}
        for account in ACCOUNTS:
            matches = [row for row in period_rows if row["account"] == account]
            refs = [row["id"] for row in matches]
            value = matches[0]["amount_inr"] if len(matches) == 1 else None
            status = "available"
            if not matches:
                status = "missing"
            elif len(matches) > 1:
                status = "duplicate"
            elif value is None:
                status = "unsupported_source"
            accounts[account] = {"value": value, "refs": refs, "status": status}
            if status != "available":
                exceptions.append({"year": year, "code": status, "account": account, "refs": refs})

        assets, liabilities, equity = (accounts[key] for key in ("assets", "liabilities", "equity"))
        balance_refs = assets["refs"] + liabilities["refs"] + equity["refs"]
        gap = None
        if all(item["value"] is not None for item in (assets, liabilities, equity)):
            gap = assets["value"] - liabilities["value"] - equity["value"]
        if gap != 0:
            exceptions.append({"year": year, "code": "balance_unavailable" if gap is None else "balance_gap",
                               "value": gap, "refs": balance_refs})

        metrics = {}
        for name, (numerator, denominator, scale) in FORMULAS.items():
            left, right = accounts[numerator], accounts[denominator]
            value, reason = None, None
            if left["value"] is None or right["value"] is None:
                reason = "missing_or_conflicting_input"
            elif right["value"] <= 0:
                reason = "nonpositive_denominator"
            else:
                value = left["value"] * scale / right["value"]
            if reason:
                exceptions.append({"year": year, "code": reason, "metric": name,
                                   "refs": left["refs"] + right["refs"]})
            metrics[name] = {
                "value": value, "reason": reason, "refs": left["refs"] + right["refs"],
                "formula": {"numerator": numerator, "denominator": denominator, "scale": scale},
            }
        annual.append({"year": year, "accounts": accounts, "metrics": metrics,
                       "balance": {"gap": gap, "refs": balance_refs}})

    return {"formula_version": "1", "rows": mapped, "annual": annual, "exceptions": exceptions,
            "provisional": bool(exceptions or any(row["review"] is None for row in mapped))}
