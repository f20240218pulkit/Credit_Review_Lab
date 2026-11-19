import json
from pathlib import Path
import logging

import pymupdf
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, model_validator

from .documents import MAX_BYTES, extract_rows, make_demo_pdf
from .engine import Mapper, Review, SourceRow, analyse
from .security import audit, clean_text
from .web import public_demo, register_workspace

DEMO = json.loads(Path(__file__).with_name("demo.json").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in DEMO["cases"]}
MAPPER = Mapper(DEMO["labels"])
app = FastAPI(title="Credit Review Lab", version="0.3.0")


class DemoReview(BaseModel):
    reviews: list[Review] = Field(default_factory=list, max_length=500)


class AnalysisRequest(DemoReview):
    years: tuple[int, int]
    rows: list[SourceRow] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_source(self):
        if len(set(self.years)) != 2 or any(not 1900 <= year <= 2200 for year in self.years):
            raise ValueError("Expected two distinct reporting years from 1900 through 2200")
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError("Source row IDs must be unique")
        if any(row.year not in self.years for row in self.rows):
            raise ValueError("Every row must belong to one of the two reporting years")
        return self


def run_analysis(request):
    known = {row.id for row in request.rows}
    identifiers = [review.row_id for review in request.reviews]
    if set(identifiers) - known:
        raise HTTPException(422, "Every review must reference an existing source row")
    if len(identifiers) != len(set(identifiers)):
        raise HTTPException(422, "Supply at most one review decision per source row")
    result = analyse(request.rows, request.reviews, MAPPER, request.years)
    return JSONResponse(TypeAdapter(dict).dump_python(result, mode="json"))


@app.get("/cases")
def list_cases():
    return [{key: value for key, value in case.items() if key != "rows"} for case in CASES.values()]


def get_demo_request(case_id, reviews):
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, "Unknown demo case")
    return AnalysisRequest(years=(2025, 2026), rows=case["rows"], reviews=reviews)


@app.get("/cases/{case_id}")
def get_demo_analysis(case_id: str):
    return run_analysis(get_demo_request(case_id, []))


@app.post("/cases/{case_id}/analyse")
def review_demo(case_id: str, body: DemoReview):
    return run_analysis(get_demo_request(case_id, body.reviews))


@app.post("/analyse")
def analyse_rows(body: AnalysisRequest):
    return run_analysis(body)


@app.get("/cases/{case_id}/statement.pdf")
def demo_statement(case_id: str):
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, "Unknown demo case")
    return Response(make_demo_pdf(case), media_type="application/pdf")


@app.post("/extract")
async def extract_statement(request: Request):
    if public_demo():
        raise HTTPException(403, "Uploads are available in the local workspace")
    return await run_in_threadpool(parse_pdf, await read_pdf(request), uploaded=True)


async def read_pdf(request):
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/pdf":
        audit("statement_media_type_rejected", logging.WARNING)
        raise HTTPException(415, "Send PDF bytes with Content-Type application/pdf")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "PDF exceeds 2 MiB")
        payload.extend(chunk)
    return bytes(payload)


def parse_pdf(payload, *, uploaded=False):
    try:
        rows = extract_rows(payload, uploaded=uploaded)
        if uploaded:
            for row in rows:
                clean_text(row.label)
        return rows
    except (ValueError, pymupdf.FileDataError) as exc:
        if uploaded:
            audit("statement_pdf_rejected", logging.WARNING)
        raise HTTPException(422, "The PDF could not be opened" if isinstance(exc, pymupdf.FileDataError)
                            else str(exc)) from exc


register_workspace(app, CASES, MAPPER, read_pdf, parse_pdf)
