from copy import deepcopy
from html import escape
from hashlib import sha256
import json
import logging
import os
import re
from pathlib import Path
from secrets import token_urlsafe
from os import getenv
from uuid import uuid4

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import pymupdf
from starlette.concurrency import run_in_threadpool

from .accounts import NO_STORE, account_owner, register_accounts
from .config import public_demo
from .documents import make_demo_pdf, serial_pdf
from .intake import csv_evidence
from .intake_jobs import register_intake_jobs
from . import object_storage
from .teams import consume_quota, membership, register_teams, signed_in
from .engine import Review
from .memos import cash_scenario, memo_pdf
from .security import CSRF_HEADER, RequestSecurity, audit, clean_text, csrf_token, valid_csrf
from . import workspace as work

COOKIE = "credit_workspace"
STATIC = Path(__file__).with_name("static")
HOME_TEMPLATE = Path(__file__).with_name("workspace.html").read_text(encoding="utf-8")
HELP_TEMPLATE = Path(__file__).with_name("help.html").read_text(encoding="utf-8")
LAYOUTS = {"CR-101": 0, "CR-102": 1, "CR-103": 2}


class ReviewRequest(Review):
    revision: int = Field(ge=1)

    _safe_reason = field_validator("reason")(clean_text)


class ArchiveRequest(BaseModel):
    revision: int = Field(ge=1)
    archived: bool


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
    register_teams(app, require_workspace_csrf, mapper)
    register_intake_jobs(app, require_workspace_csrf)
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
            + csrf_token(csrf_key(request)) + '"><button class="button-secondary" type="submit">Sign out</button></form>'
            if account_owner(request) else '<a class="button-link button-warm" href="/signin">Sign in</a>'
        )
        uploads_enabled = can_upload(request)
        upload_access = (
            '<div class="upload-access"><div><h3>Start your own review</h3>'
            '<p>Sign in to upload a borrower statement pack.</p></div>'
            '<a class="button-link button-warm" href="/signin">Sign in to upload</a></div>'
            if not uploads_enabled else ""
        )
        page = homepage.replace(" data-upload-mode", "" if uploads_enabled else " hidden")
        page = page.replace("<!-- upload-access -->", upload_access)
        if object_storage.enabled() and account_owner(request):
            page = page.replace("<body>", '<body data-private-intake>')
        page = page.replace(" data-team-guard", "" if account_owner(request) else " hidden")
        return HTMLResponse(page.replace("<!-- account-action -->", account_action), headers=NO_STORE)

    @app.get("/help", response_class=HTMLResponse, include_in_schema=False)
    def help_page():
        return HTMLResponse(HELP_TEMPLATE)

    @app.get("/settings")
    def settings(request: Request):
        return JSONResponse({"uploads": can_upload(request)}, headers=NO_STORE)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    def ready():
        try:
            with work.database() as db:
                db.execute("SELECT id FROM intake_jobs LIMIT 1").fetchone()
        except Exception:
            audit("readiness_database_unavailable", logging.WARNING)
            return JSONResponse({"status": "unavailable"}, status_code=503, headers=NO_STORE)
        return JSONResponse({"status": "ready"}, headers=NO_STORE)

    @app.get("/workspace")
    def list_workspace(request: Request, response: Response):
        token = session(request, response)
        response.headers[CSRF_HEADER] = csrf_token(csrf_key(request) or token)
        with work.database() as db:
            states = [(json.loads(item[0]), item[1]) for item in work.list_cases(db, token)]
        return [{**{key: state[key] for key in ("id", "name", "demo")}, "organization": org,
                 "archived": state.get("archived", False),
                 "status": state.get("workflow", {}).get("status", "draft")}
                for state, org in states]

    @app.post("/workspace/demo/{demo_id}", status_code=201, dependencies=[Depends(require_workspace_csrf)])
    def create_demo(demo_id: str, request: Request, response: Response):
        case = cases.get(demo_id)
        if case is None:
            raise HTTPException(404, "Unknown demo")
        token = session(request, response)
        with work.database(write=True) as db:
            consume_quota(db, "demo:" + token, 20)
        pdf = make_demo_pdf(case, LAYOUTS[demo_id])
        rows = parse_pdf(pdf)
        with work.database(write=True) as db:
            state = work.create_case(db, token, case["name"], pdf, rows, demo_id)
        return work.view(state, mapper)

    @app.post("/workspace/upload", status_code=201, dependencies=[Depends(require_workspace_csrf)])
    async def upload(request: Request, response: Response, name: str = Query(min_length=1, max_length=80),
                     organization: str | None = Query(default=None, max_length=32)):
        if not can_upload(request):
            raise HTTPException(403, "Sign in to upload statement packs")
        token = session(request, response)
        try:
            name = clean_text(name).strip()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not name:
            raise HTTPException(422, "Borrower name is required")
        if getenv("CREDIT_PILOT_INVITE_ONLY") == "1" and not organization:
            raise HTTPException(403, "Choose an invited team workspace for uploads")
        if organization:
            signed_in(request)
        with work.database(write=True) as db:
            if organization:
                membership(db, token, organization, {"admin", "analyst"})
                if object_storage.enabled():
                    raise HTTPException(409, "Use private intake for new team statements")
            consume_quota(db, "upload:" + token, 30)
        pdf, original = await intake(request)

        def create():
            rows = parse_pdf(pdf, uploaded=True)
            with work.database(write=True) as db:
                if organization:
                    membership(db, token, organization, {"admin", "analyst"})
                state = work.create_case(db, token, name, pdf, rows, organization=organization)
                if original is not None:
                    save_csv(db, state, original)
                result = work.view(work.load_case(db, token, state["id"]), mapper)
            audit("statement_uploaded")
            return result

        return await run_in_threadpool(create)

    async def intake(request):
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type == "text/csv":
            source = await request.body()
            try:
                return await run_in_threadpool(csv_evidence, source), source
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        return await read_pdf(request), None

    def save_csv(db, state, data):
        db.execute("""INSERT INTO original_uploads VALUES (?, ?, 'text/csv', ?)
            ON CONFLICT(case_id, document_id) DO NOTHING""", (state["id"], state["document"], data))
        state["versions"][-1].update(source_type="csv", original_sha256=sha256(data).hexdigest())
        work.store_case(db, state["id"], state)

    @app.get("/workspace/{case_id}/original/{document_id}")
    def original_upload(case_id: str, document_id: str, request: Request):
        with work.database() as db:
            work.load_case(db, owner(request), case_id)
            row = db.execute("SELECT media_type, payload FROM original_uploads WHERE case_id=? AND document_id=?",
                             (case_id, document_id)).fetchone()
            remote = db.execute("""SELECT object_key, object_version FROM object_sources
                WHERE case_id=? AND document_id=? AND media_type='text/csv'""", (case_id, document_id)).fetchone()
        if row:
            payload = row[1]
        elif remote:
            payload = read_private_file(remote, "text/csv")
        else:
            raise HTTPException(404, "Original CSV unavailable")
        return Response(payload, media_type="text/csv", headers={
            "Content-Disposition": "attachment; filename=statement.csv", "Cache-Control": "private, no-store"})

    @app.get("/workspace/{case_id}")
    def get_case(case_id: str, request: Request):
        with work.database() as db:
            return work.view(work.load_case(db, owner(request), case_id), mapper)

    @app.post("/workspace/{case_id}/archive", dependencies=[Depends(require_workspace_csrf)])
    def archive(case_id: str, body: ArchiveRequest, request: Request):
        actor = signed_in(request)
        with work.database(write=True) as db:
            state = work.load_case(db, actor, case_id, lock=True)
            if state.get("access", {}).get("role") == "reviewer":
                raise HTTPException(403, "Only analysts and administrators can archive cases")
            work.check_revision(state, body.revision)
            if state.get("workflow", {}).get("status") == "in_review":
                raise HTTPException(409, "Complete the assigned review before archiving")
            if state.get("archived", False) != body.archived:
                state["archived"] = body.archived
                state["revision"] += 1
                state["history"].append({"action": "archived" if body.archived else "restored",
                                         "actor": actor, "at": work.now()})
                work.store_case(db, case_id, state)
            return work.view(state, mapper)

    @app.post("/workspace/{case_id}/reviews", dependencies=[Depends(require_workspace_csrf)])
    def add_review(case_id: str, body: ReviewRequest, request: Request):
        with work.database(write=True) as db:
            state = work.load_case(db, owner(request), case_id, lock=True)
            work.require_edit(state)
            work.check_revision(state, body.revision)
            work.review_row(state, Review.model_validate(body.model_dump()), mapper, account_owner(request))
            work.store_case(db, case_id, state)
            return work.view(state, mapper)

    @app.post("/workspace/{case_id}/replacement", dependencies=[Depends(require_workspace_csrf)])
    async def replace(case_id: str, request: Request, revision: int, demo: bool = False):
        token = owner(request)
        if not demo and not can_upload(request):
            raise HTTPException(403, "Sign in to upload a replacement statement")
        with work.database(write=True) as db:
            state = work.load_case(db, token, case_id)
            work.require_edit(state, replacement=True)
            work.check_revision(state, revision)
            if not demo:
                if state.get("access") and object_storage.enabled():
                    raise HTTPException(409, "Use private intake for team replacements")
                if getenv("CREDIT_PILOT_INVITE_ONLY") == "1" and not state.get("access"):
                    raise HTTPException(403, "Choose an invited team workspace for uploads")
                consume_quota(db, "upload:" + token, 30)
        original = None
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
            pdf, original = await intake(request)

        def commit():
            rows = parse_pdf(pdf, uploaded=not demo)
            with work.database(write=True) as db:
                current = work.load_case(db, token, case_id, lock=True)
                work.require_edit(current, replacement=True)
                work.check_revision(current, revision)
                updated = work.replace_document(db, current, pdf, rows, mapper)
                if original is not None:
                    save_csv(db, updated, original)
                result = work.view(updated, mapper)
            audit("statement_replaced")
            return result

        return await run_in_threadpool(commit)

    def read_private_file(reference, media):
        try:
            return object_storage.read_version(reference[0], reference[1], object_storage.max_size(media))
        except Exception:
            audit("private_document_unavailable", logging.WARNING)
            raise HTTPException(503, "The statement is temporarily unavailable") from None

    def get_document(case_id: str, document_id: str, request: Request):
        with work.database() as db:
            work.load_case(db, owner(request), case_id)
            row = db.execute("SELECT pdf FROM documents WHERE case_id=? AND id=?", (case_id, document_id)).fetchone()
            if row is not None and not row[0]:
                remote = db.execute("""SELECT object_key, object_version FROM object_sources
                    WHERE case_id=? AND document_id=? AND media_type='application/pdf'""",
                    (case_id, document_id)).fetchone()
            else:
                remote = None
        if remote:
            return read_private_file(remote, "application/pdf")
        if row is None or not row[0]:
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
            state = work.load_case(db, owner(request), case_id, lock=True)
            work.require_edit(state)
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

