from copy import deepcopy
from html import escape
import json
import logging
import os
import re
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import pymupdf
from starlette.concurrency import run_in_threadpool

from .accounts import NO_STORE, account_owner, register_accounts
from .documents import make_demo_pdf, serial_pdf
from .engine import Review
from .memos import cash_scenario, memo_pdf
from .security import CSRF_HEADER, RequestSecurity, audit, clean_text, csrf_token, valid_csrf
from . import workspace as work

COOKIE = "credit_workspace"
def public_demo():
    return os.environ.get("VERCEL") == "1" or os.environ.get("CREDIT_PUBLIC_DEMO") == "1"


STATIC = Path(__file__).with_name("static")
HOME_TEMPLATE = Path(__file__).with_name("workspace.html").read_text(encoding="utf-8")
LAYOUTS = {"CR-101": 0, "CR-102": 1, "CR-103": 2}


class ReviewRequest(Review):
    revision: int = Field(ge=1)

    _safe_reason = field_validator("reason")(clean_text)


class MemoRequest(BaseModel):
    revision: int = Field(ge=1)
    decline_percent: int = Field(default=15, ge=0, le=100)
    notes: str = Field(default="", max_length=1800)

    _safe_notes = field_validator("notes")(clean_text)


def owner(request):
    token = request.cookies.get(COOKIE, "")
    return account_owner(request) or (token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else "")


def csrf_key(request):
    return request.scope.get("session", {}).get("session_id") if account_owner(request) else owner(request)


def require_workspace_csrf(request: Request):
    if not valid_csrf(request, csrf_key(request), request.headers.get(CSRF_HEADER)):
        audit("workspace_csrf_rejected", logging.WARNING)
        raise HTTPException(403, "Request verification failed. Reload the workspace and try again.")


def can_upload(request):
    return not public_demo() or bool(account_owner(request))


def session(request, response):
    token = owner(request) or token_urlsafe(32)
    if not owner(request):
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                            secure=os.environ.get("VERCEL") == "1" or os.environ.get("CREDIT_SECURE_COOKIE") == "1", max_age=30 * 86400)
    return token


def register_workspace(app, cases, mapper, read_pdf, parse_pdf):
    register_accounts(app)
    app.add_middleware(RequestSecurity)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    launchers = "".join(
        f'<button type="button" data-demo="{escape(case_id)}" disabled>Try {escape(case["name"])}</button>'
        for case_id, case in cases.items()
    )
    homepage = HOME_TEMPLATE.replace("<!-- demo-launchers -->", launchers)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request):
        account_action = (
            '<form action="/auth/logout" method="post"><input type="hidden" name="csrf" value="'
            + csrf_token(csrf_key(request)) + '"><button type="submit">Sign out</button></form>'
            if account_owner(request) else '<a href="/signin">Sign in</a>'
        )
        page = homepage.replace(" data-upload-mode", "" if can_upload(request) else " hidden")
        return HTMLResponse(page.replace("<!-- account-action -->", account_action), headers=NO_STORE)

    @app.get("/settings")
    def settings(request: Request):
        return JSONResponse({"uploads": can_upload(request)}, headers=NO_STORE)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/workspace")
    def list_workspace(request: Request, response: Response):
        token = session(request, response)
        response.headers[CSRF_HEADER] = csrf_token(csrf_key(request) or token)
        with work.database() as db:
            states = [json.loads(item[0]) for item in db.execute(
                "SELECT state FROM cases WHERE owner=? ORDER BY rowid DESC", (token,))]
        return [{key: state[key] for key in ("id", "name", "demo")} for state in states]

    @app.post("/workspace/demo/{demo_id}", status_code=201, dependencies=[Depends(require_workspace_csrf)])
    def create_demo(demo_id: str, request: Request, response: Response):
        case = cases.get(demo_id)
        if case is None:
            raise HTTPException(404, "Unknown demo")
        token = session(request, response)
        pdf = make_demo_pdf(case, LAYOUTS[demo_id])
        rows = parse_pdf(pdf)
        with work.database(write=True) as db:
            state = work.create_case(db, token, case["name"], pdf, rows, demo_id)
        return work.view(state, mapper)

    @app.post("/workspace/upload", status_code=201, dependencies=[Depends(require_workspace_csrf)])
    async def upload(request: Request, response: Response, name: str = Query(min_length=1, max_length=80)):
        if not can_upload(request):
            raise HTTPException(403, "Sign in to upload statement packs")
        token = session(request, response)
        try:
            name = clean_text(name).strip()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not name:
            raise HTTPException(422, "Borrower name is required")
        pdf = await read_pdf(request)

        def create():
            rows = parse_pdf(pdf, uploaded=True)
            with work.database(write=True) as db:
                state = work.create_case(db, token, name, pdf, rows)
            return work.view(state, mapper)

        return await run_in_threadpool(create)

    @app.get("/workspace/{case_id}")
    def get_case(case_id: str, request: Request):
        with work.database() as db:
            return work.view(work.load_case(db, owner(request), case_id), mapper)

    @app.post("/workspace/{case_id}/reviews", dependencies=[Depends(require_workspace_csrf)])
    def add_review(case_id: str, body: ReviewRequest, request: Request):
        with work.database(write=True) as db:
            state = work.load_case(db, owner(request), case_id)
            work.check_revision(state, body.revision)
            work.review_row(state, Review.model_validate(body.model_dump()), mapper)
            work.store_case(db, case_id, state)
            return work.view(state, mapper)

    @app.post("/workspace/{case_id}/replacement", dependencies=[Depends(require_workspace_csrf)])
    async def replace(case_id: str, request: Request, revision: int, demo: bool = False):
        token = owner(request)
        if not demo and not can_upload(request):
            raise HTTPException(403, "Sign in to upload a replacement statement")
        with work.database() as db:
            state = work.load_case(db, token, case_id)
            work.check_revision(state, revision)
        if demo:
            if state["demo"] != "CR-102":
                raise HTTPException(422, "The revised sample belongs to Meridian Packaging")
            revised = deepcopy(cases["CR-102"])
            for row in revised["rows"]:
                if row["id"] == "2026-09":
                    row["value"] = 760
                if row["id"] == "2026-10":
                    row["value"] = 800
            pdf = make_demo_pdf(revised, LAYOUTS["CR-102"])
        else:
            pdf = await read_pdf(request)

        def commit():
            rows = parse_pdf(pdf, uploaded=not demo)
            with work.database(write=True) as db:
                current = work.load_case(db, token, case_id)
                work.check_revision(current, revision)
                updated = work.replace_document(db, current, pdf, rows, mapper)
                result = work.view(updated, mapper)
            audit("statement_replaced")
            return result

        return await run_in_threadpool(commit)

    def get_document(case_id: str, document_id: str, request: Request):
        with work.database() as db:
            work.load_case(db, owner(request), case_id)
            row = db.execute("SELECT pdf FROM documents WHERE case_id=? AND id=?", (case_id, document_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Document unavailable")
        return row[0]

    @app.get("/workspace/{case_id}/documents/{document_id}/page/{number}")
    @serial_pdf
    def page_preview(case_id: str, document_id: str, number: int, request: Request, highlight: str | None = None):
        pdf = get_document(case_id, document_id, request)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            if not 1 <= number <= len(document):
                raise HTTPException(404, "Page unavailable")
            page = document[number - 1]
            if highlight:
                row = next((row for row in parse_pdf(pdf) if row.id == highlight and row.page == number), None)
                if row is None:
                    raise HTTPException(404, "Source line unavailable")
                page.draw_rect(pymupdf.Rect(row.bbox), color=(0.85, 0.45, 0), fill=(1, 0.78, 0.15),
                               fill_opacity=0.28, width=0.8)
            scale = min(2, 1400 / max(page.rect.width, page.rect.height))
            preview = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return Response(preview.tobytes("png"), media_type="image/png",
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/workspace/{case_id}/source/{document_id}")
    def download_source(case_id: str, document_id: str, request: Request):
        return Response(get_document(case_id, document_id, request), media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=statement.pdf",
                                 "Cache-Control": "private, no-store"})

    @app.get("/workspace/{case_id}/scenario")
    def scenario(case_id: str, request: Request, decline: int = Query(ge=0, le=100)):
        with work.database() as db:
            state = work.load_case(db, owner(request), case_id)
            return cash_scenario(work.result(state, mapper), decline)

    @app.post("/workspace/{case_id}/snapshots", status_code=201, dependencies=[Depends(require_workspace_csrf)])
    def snapshot(case_id: str, body: MemoRequest, request: Request):
        with work.database(write=True) as db:
            state = work.load_case(db, owner(request), case_id)
            work.check_revision(state, body.revision)
            analysis = work.result(state, mapper)
            entry = {"id": uuid4().hex, "at": work.now(), "revision": state["revision"],
                     "status": "draft" if analysis["provisional"] else "reviewed"}
            data = {**entry, "name": state["name"], "document": state["document"], "analysis": analysis,
                    "history": state["history"], "notes": body.notes,
                    "scenario": cash_scenario(analysis, body.decline_percent)}
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (case_id, entry["id"], json.dumps(data)))
            state["snapshots"].append(entry)
            work.store_case(db, case_id, state)
            return entry

    @app.get("/workspace/{case_id}/snapshots/{snapshot_id}.{format}")
    def export(case_id: str, snapshot_id: str, format: str, request: Request):
        with work.database() as db:
            work.load_case(db, owner(request), case_id)
            record = db.execute("SELECT data FROM snapshots WHERE case_id=? AND id=?", (case_id, snapshot_id)).fetchone()
        if record is None or format not in {"pdf", "json"}:
            raise HTTPException(404, "Snapshot unavailable")
        headers = {"Content-Disposition": f"attachment; filename=credit-memo-{snapshot_id}.{format}",
                   "Cache-Control": "private, no-store"}
        data = json.loads(record[0])
        if format == "pdf":
            return Response(memo_pdf(data), media_type="application/pdf", headers=headers)
        return JSONResponse(data, headers=headers)

