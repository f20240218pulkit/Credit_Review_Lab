"""Extract line evidence from the supported synthetic text-PDF layout."""
from functools import wraps
import hashlib
import re
from threading import RLock

import pymupdf

from .engine import SourceRow

HEADERS = (
    re.compile(r"Fiscal year (\d{4}) \| ([A-Z]{3}) \| (units|thousands)"),
    re.compile(r"FY (\d{4}) / ([A-Z]{3}) / (units|thousands)"),
    re.compile(r"Reporting period: (\d{4}) - ([A-Z]{3}) (units|thousands)"),
)
ENTRY = re.compile(r"(.+?)\s*\|\s*(-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,4})?)")
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 10
MAX_OBJECTS = 10000
ACTIVE_ACTIONS = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition"}
ACTIVE_ANNOTATIONS = {"/FileAttachment", "/RichMedia", "/Screen", "/Sound", "/Movie", "/3D", "/Widget"}


ENTRY_TABULAR = re.compile(r"(.+?)\s{2,}(-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,4})?)")


PDF_LOCK = RLock()


def serial_pdf(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with PDF_LOCK:
            return function(*args, **kwargs)
    return locked


@serial_pdf
def make_demo_pdf(case, layout=0):
    with pymupdf.open() as document:
        for year in (2025, 2026):
            entries = [row for row in case["rows"] if row["year"] == year]
            for currency in dict.fromkeys(row["currency"] for row in entries):
                page = document.new_page()
                page.insert_text((45, 55), f'{case["name"]} - Synthetic statement', fontsize=16)
                headings = [f"Fiscal year {year} | {currency} | thousands",
                            f"FY {year} / {currency} / units",
                            f"Reporting period: {year} - {currency} thousands"]
                page.insert_text((45, 85), headings[layout])
                for index, row in enumerate(item for item in entries if item["currency"] == currency):
                    label = row["label"].replace("’", "'")
                    amount = row["value"] * (1000 if layout == 1 else 1)
                    separator = "     " if layout == 1 else " | "
                    page.insert_text((45, 120 + index * 24), f"{label}{separator}{amount:,}")
        return document.tobytes(no_new_id=True)


def check_upload(document):
    if document.xref_length() > MAX_OBJECTS:
        raise ValueError("PDF exceeds the object limit")
    if (document.is_form_pdf or document.embfile_count()
            or document.xref_get_key(document.pdf_catalog(), "Names/JavaScript")[0] != "null"):
        raise ValueError("Upload a static statement PDF without forms, attachments or scripts")
    for xref in range(1, document.xref_length()):
        action = document.xref_get_key(xref, "S")[1]
        subtype = document.xref_get_key(xref, "Subtype")[1]
        if (action in ACTIVE_ACTIONS or subtype in ACTIVE_ANNOTATIONS
                or any(document.xref_get_key(xref, key)[0] != "null"
                       for key in ("JS", "AA", "OpenAction", "RichMediaContent"))):
            raise ValueError("Upload a static statement PDF without active content")


@serial_pdf
def extract_rows(payload, *, uploaded=False):
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_BYTES:
        raise ValueError("Expected a PDF of at most 2 MiB")
    document_id = hashlib.sha256(payload).hexdigest()
    rows = []
    with pymupdf.open(stream=payload, filetype="pdf") as document:
        if document.needs_pass or not 1 <= len(document) <= MAX_PAGES:
            raise ValueError("Expected an unencrypted PDF with 1 to 10 pages")
        if uploaded:
            check_upload(document)
        for number, page in enumerate(document, start=1):
            lines = [
                ("".join(span["text"] for span in line["spans"]).strip(), line["bbox"])
                for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
                for line in block.get("lines", [])
            ]
            headers = [(index, match) for text, _ in lines for index, pattern in enumerate(HEADERS)
                       if (match := pattern.fullmatch(text))]
            if len(headers) != 1:
                raise ValueError(f"Page {number} needs one reporting header in the supported layout")
            layout, header = headers[0]
            year, currency, unit = header.groups()
            entry = ENTRY_TABULAR if layout == 1 else ENTRY
            start_count = len(rows)
            for line, (text, bbox) in enumerate(lines, start=1):
                if HEADERS[layout].fullmatch(text) or (layout != 1 and "|" not in text):
                    continue
                if layout == 1 and not re.search(r"\s{2,}", text):
                    continue
                match = entry.fullmatch(text)
                if match is None:
                    raise ValueError(f"Page {number}, line {line} has an unrecognised financial row")
                label, value = match.groups()
                if len(rows) == 500:
                    raise ValueError("PDF exceeds 500 financial rows")
                rows.append(SourceRow(id=f"{document_id[:16]}-{number}-{line}", year=int(year),
                                      label=label.strip(), value=value.replace(",", ""), unit=unit,
                                      currency=currency, document=document_id, line=line,
                                      page=number, bbox=bbox))
            if len(rows) == start_count:
                raise ValueError(f"Page {number} has no financial rows in the supported layout")
    return rows
