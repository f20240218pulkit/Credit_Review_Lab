"""Durable, version-pinned upload jobs with lease-based processing."""
from hashlib import sha256
import logging
from time import time
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from .intake import csv_evidence
from .security import audit, clean_text
from .teams import consume_quota, membership, signed_in
from . import object_storage, workspace as work

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


class Intent(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    organization: str = Field(pattern=r"^[a-f0-9]{32}$")
    media_type: Literal["application/pdf", "text/csv"]
    size: int = Field(ge=1)
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    revision: int | None = Field(default=None, ge=1)
    _safe_name = field_validator("name")(clean_text)

    @model_validator(mode="after")
    def replacement_fields(self):
        if (self.case_id is None) != (self.revision is None):
            raise ValueError("Replacement intake requires both a case and a revision")
        return self


def user_job(db, account, job_id):
    row = db.execute("""SELECT j.id, j.org_id, j.object_key, j.media_type, j.checksum, j.byte_count,
            j.status, j.deadline, j.error, j.target_case_id
        FROM intake_jobs j JOIN memberships m ON m.org_id=j.org_id AND m.account=j.account
        WHERE j.id=? AND j.account=?""", (job_id, account)).fetchone()
    if not row:
        raise HTTPException(404, "Upload unavailable")
    return row


def job_status(row):
    phase = row[6]
    if phase == "awaiting_upload" and row[7] <= int(time()):
        phase = "expired"
    return {"id": row[0], "status": phase, "error": row[8],
            "case_id": (row[9] or row[0]) if phase == "complete" else None}


def register_intake_jobs(app, csrf):
    mutation = [Depends(csrf)]

    @app.post("/workspace/intake", status_code=201, dependencies=mutation)
    def intent(request: Request, body: Intent):
        account = signed_in(request)
        if not object_storage.enabled():
            raise HTTPException(404, "Private intake is unavailable")
        name = body.name.strip()
        if not name or body.size > object_storage.max_size(body.media_type):
            raise HTTPException(422, "Enter a borrower name and select a supported file within the limit")
        job_id = uuid4().hex
        key = f"pilot-intake/{body.organization}/{job_id}"
        now = int(time())
        with work.database(write=True) as db:
            membership(db, account, body.organization, {"admin", "analyst"})
            if body.case_id:
                target = work.load_case(db, account, body.case_id)
                validate_replacement(target, body.organization, body.revision)
            consume_quota(db, "upload:" + account, 30)
            db.execute("""INSERT INTO intake_jobs
                (id, account, org_id, borrower, media_type, checksum, byte_count, object_key, status, available_at, deadline, target_case_id, target_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_upload', ?, ?, ?, ?)""",
                (job_id, account, body.organization, name, body.media_type, body.checksum, body.size, key,
                 now, now + 900, body.case_id, body.revision))
        try:
            target = object_storage.authorize(key, body.media_type, body.checksum)
        except Exception:
            audit("intake_storage_authorization_failed", logging.WARNING)
            raise HTTPException(503, "The upload service is temporarily unavailable") from None
        return {"id": job_id, "target": target}

    @app.post("/workspace/intake/{job_id}/finalize", dependencies=mutation)
    def finalize(job_id: str, request: Request):
        account = signed_in(request)
        if not object_storage.enabled():
            raise HTTPException(404, "Private intake is unavailable")
        if len(job_id) != 32:
            raise HTTPException(404, "Upload unavailable")
        with work.database() as db:
            row = user_job(db, account, job_id)
        if row[6] != "awaiting_upload":
            return job_status(row)
        if row[7] <= int(time()):
            raise HTTPException(410, "Upload intent expired. Start a new upload.")
        try:
            meta = object_storage.describe(row[2])
        except Exception:
            audit("intake_storage_verification_unavailable", logging.WARNING)
            raise HTTPException(409, "The uploaded object could not be verified. Try again.") from None
        version = meta.get("VersionId")
        if (not version or version == "null" or meta.get("ContentLength") != row[5]
                or meta.get("ContentType") != row[3] or meta.get("Metadata", {}).get("sha256") != row[4]):
            raise HTTPException(422, "Storage version or file metadata did not match this upload")
        with work.database(write=True) as db:
            db.execute("""UPDATE intake_jobs SET object_version=?, status='queued', available_at=?
                       WHERE id=? AND status='awaiting_upload' AND deadline>?""",
                       (version, int(time()), job_id, int(time())))
            return job_status(user_job(db, account, job_id))

    @app.get("/workspace/intake/{job_id}")
    def status(job_id: str, request: Request):
        account = signed_in(request)
        with work.database() as db:
            return job_status(user_job(db, account, job_id))


def validate_replacement(state, organization, revision):
    if state.get("access", {}).get("organization") != organization:
        raise HTTPException(404, "Team case unavailable")
    work.require_edit(state, replacement=True)
    work.check_revision(state, revision)


def run_one(parse_pdf, mapper):
    now = int(time())
    with work.database(write=True) as db:
        db.execute("""UPDATE intake_jobs SET status='failed', error='Processing timed out.'
                   WHERE status='processing' AND available_at<=? AND attempts>=?""", (now, MAX_ATTEMPTS))
        lock = " FOR UPDATE SKIP LOCKED" if isinstance(db, work.PostgresConnection) else ""
        row = db.execute("""SELECT id, account, org_id, borrower, media_type, checksum, byte_count, object_key,
                          object_version, attempts, target_case_id, target_revision FROM intake_jobs
                          WHERE status IN ('queued', 'processing') AND available_at<=? AND attempts<?
                          ORDER BY available_at, id LIMIT 1""" + lock, (now, MAX_ATTEMPTS)).fetchone()
        if not row:
            return False
        job_id, account, org, borrower, media, checksum, size, key, version, attempts, target, revision = row
        attempt = attempts + 1
        db.execute("UPDATE intake_jobs SET status='processing', attempts=?, available_at=? WHERE id=?",
                   (attempt, now + LEASE_SECONDS, job_id))

    retry = False
    try:
        payload = object_storage.read_version(key, version, object_storage.max_size(media))
        if len(payload) != size or sha256(payload).hexdigest() != checksum:
            raise ValueError("The uploaded bytes did not match the expected checksum")
        csv = media == "text/csv"
        pdf = csv_evidence(payload) if csv else payload
        rows = parse_pdf(pdf, uploaded=True)
        with work.database(write=True) as db:
            claimed = db.execute("""UPDATE intake_jobs SET status='complete', error=NULL
                WHERE id=? AND attempts=? AND status='processing' AND available_at>? RETURNING id""",
                (job_id, attempt, int(time()))).fetchone()
            if not claimed:
                return True
            membership(db, account, org, {"admin", "analyst"})
            case_id = target or job_id
            stored_pdf = pdf if csv else b""
            if target:
                prior = work.load_case(db, account, target, lock=True)
                validate_replacement(prior, org, revision)
                state = work.replace_document(db, prior, stored_pdf, rows, mapper)
            else:
                state = work.create_case(db, account, borrower, stored_pdf, rows, organization=org, case_id=case_id)
            db.execute("""INSERT INTO object_sources VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id, document_id) DO NOTHING""", (case_id, state["document"], key, version, media))
            if csv:
                state["versions"][-1].update(source_type="csv", original_sha256=checksum)
                work.store_case(db, case_id, state)
        audit("intake_processing_completed")
        return True
    except (ValueError, HTTPException) as exc:
        error = str(exc.detail if isinstance(exc, HTTPException) else exc)
    except Exception:
        retry = attempt < MAX_ATTEMPTS
        error = "Statement processing failed. Try uploading the file again."
    with work.database(write=True) as db:
        db.execute("""UPDATE intake_jobs SET status=?, error=?, available_at=?
                   WHERE id=? AND attempts=? AND status='processing'""",
                   ("queued" if retry else "failed", error, int(time()) + 15 * attempt, job_id, attempt))
    audit("intake_processing_retried" if retry else "intake_processing_failed", logging.WARNING)
    return True
