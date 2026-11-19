"""Bounded manual statement intake with a derived, citeable evidence view."""
import csv
from decimal import Decimal
from hashlib import sha256
from io import StringIO
import re

import pymupdf

from .documents import serial_pdf
from .security import clean_text

CSV_HEADER = ["year", "label", "value", "unit", "currency"]
CSV_LIMIT = 128 * 1024
ROWS_PER_PAGE = 28


@serial_pdf
def csv_evidence(payload):
    if not payload or len(payload) > CSV_LIMIT:
        raise ValueError("Use a UTF-8 CSV no larger than 128 KiB")
    try:
        reader = csv.reader(StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        if next(reader, None) != CSV_HEADER:
            raise ValueError("CSV headers must be year,label,value,unit,currency")
        periods = {}
        for fields in reader:
            if not fields:
                continue
            if len(fields) != 5:
                raise ValueError(f"CSV line {reader.line_num} must have five values")
            year, label, amount, unit, currency = [value.strip() for value in fields]
            label = clean_text(label).translate(str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"}))
            if (not re.fullmatch(r"20\d{2}", year)
                    or not re.fullmatch(r"[A-Za-z0-9 .()&+/'%_-]{1,75}", label)
                    or not re.fullmatch(r"-?\d{1,15}(?:\.\d{1,4})?", amount)
                    or unit not in {"units", "thousands"} or currency != "INR"):
                raise ValueError(f"CSV line {reader.line_num} has an unsupported year, label, amount, unit or currency")
            key = (year, unit)
            periods.setdefault(key, []).append((label, Decimal(amount), reader.line_num))
            if sum(len(rows) for rows in periods.values()) > 240:
                raise ValueError("CSV may contain up to 240 financial rows")
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("Upload a valid UTF-8 CSV") from error
    if len({year for year, _ in periods}) != 2:
        raise ValueError("CSV must contain two reporting years")
    if sum((len(rows) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE for rows in periods.values()) > 10:
        raise ValueError("CSV requires more than 10 evidence pages")

    with pymupdf.open() as result:
        for (year, unit), rows in sorted(periods.items()):
            for start in range(0, len(rows), ROWS_PER_PAGE):
                page = result.new_page()
                page.insert_text((42, 43), "CSV evidence view - derived from analyst-supplied values", fontsize=11)
                page.insert_text((42, 62), "CSV SHA-256 " + sha256(payload).hexdigest(), fontsize=8)
                page.insert_text((42, 86), f"Fiscal year {year} | INR | {unit}", fontsize=12)
                for index, (label, amount, csv_line) in enumerate(rows[start:start + ROWS_PER_PAGE]):
                    y = 111 + index * 24
                    page.insert_text((42, y), f"{label} | {amount:f}", fontsize=9)
                    page.insert_text((42, y + 10), f"Original CSV line {csv_line}", fontsize=7)
        return result.tobytes(no_new_id=True)
