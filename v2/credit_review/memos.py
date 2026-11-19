from decimal import Decimal
from textwrap import wrap

import pymupdf

from .documents import serial_pdf


def cash_scenario(analysis, decline):
    period = analysis["annual"][-1]
    cash = period["accounts"]["operating_cash"]
    debt = period["accounts"]["debt_service"]
    base = period["metrics"]["operating_cash_coverage"]["value"]
    stressed = None
    if base is not None:
        stressed = str(Decimal(cash["value"]) * (1 - Decimal(decline) / 100) / Decimal(debt["value"]))
    return {"year": period["year"], "decline_percent": decline, "baseline": base,
            "stressed": stressed, "refs": cash["refs"] + debt["refs"]}


@serial_pdf
def memo_pdf(snapshot):
    analysis = snapshot["analysis"]
    document = pymupdf.open()
    lines = [
        "CREDIT REVIEW LAB | SME CREDIT MEMO",
        f'{snapshot["name"]} | {snapshot["status"].upper()}',
        f'Created {snapshot["at"]} | Revision {snapshot["revision"]}',
        f'Statement SHA-256 {snapshot["document"]}',
    ]
    sign_off = snapshot.get("sign_off")
    if sign_off:
        lines.extend([
            f'Reviewer: {sign_off["name"]} | Signed off {sign_off["at"]}',
            f'Input revision {sign_off["input_revision"]} | Formula version {analysis["formula_version"]}',
            sign_off["notes"],
        ])
    rows = {row["id"]: row for row in analysis["rows"]}
    for period in analysis["annual"]:
        lines.append(f'FY{period["year"]}')
        for key, metric in period["metrics"].items():
            value = f'{Decimal(metric["value"]):.2f}' if metric["value"] is not None else metric["reason"]
            citations = ", ".join(f'p{rows[ref]["page"]} line {rows[ref]["line"]}' for ref in metric["refs"])
            lines.append(f'{key.replace("_", " ").title()}: {value} | {citations}')
    lines.append("Open exceptions")
    for item in analysis["exceptions"]:
        citations = ", ".join(f'p{rows[ref]["page"]} line {rows[ref]["line"]}' for ref in item["refs"])
        lines.append(f'FY{item["year"]} {item["code"]} {item.get("account", item.get("metric", ""))} | {citations}')
    scenario = snapshot["scenario"]
    if scenario["stressed"] is not None:
        lines.append(f'Cash coverage after {scenario["decline_percent"]}% stress: {Decimal(scenario["stressed"]):.2f}x')
    lines.extend(["Analyst commentary", snapshot["notes"], "Supporting evidence is included in the JSON export."])

    try:
        wrapped = [part for line in lines for part in wrap(line, width=102) or [""]]
        for start in range(0, len(wrapped), 43):
            page = document.new_page()
            for offset, line in enumerate(wrapped[start:start + 43]):
                page.insert_text((42, 48 + offset * 17), line, fontsize=9)
        return document.tobytes(no_new_id=True)
    finally:
        document.close()
