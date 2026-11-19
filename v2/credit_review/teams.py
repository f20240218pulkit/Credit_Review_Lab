"""Organization membership, invitations and four-eyes review."""
from hashlib import sha256
import json
import os
from secrets import token_urlsafe
from time import time
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .accounts import account_owner, email_digest
from .memos import cash_scenario
from .security import audit, clean_text
from . import workspace as work


class OrganizationInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    _validate = field_validator("name")(clean_text)


class InviteInput(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: Literal["analyst", "reviewer"]
    name: str = Field(min_length=1, max_length=100)
    _validate = field_validator("name")(clean_text)


class AcceptInput(BaseModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class ReviewAction(BaseModel):
    action: Literal["submit", "request_changes", "sign_off", "recall"]
    revision: int = Field(ge=1)
    reviewer: str | None = Field(default=None, max_length=80)
    notes: str = Field(default="", max_length=1800)
    decline_percent: int = Field(default=15, ge=0, le=100)
    _validate = field_validator("notes")(clean_text)


def signed_in(request):
    account = account_owner(request)
    if not account:
        raise HTTPException(401, "Sign in to use team workspaces")
    return account


def membership(db, account, org_id, roles=()):
    row = db.execute("SELECT role FROM memberships WHERE org_id=? AND account=?", (org_id, account)).fetchone()
    if not row:
        raise HTTPException(404, "Team unavailable")
    if roles and row[0] not in roles:
        raise HTTPException(403, "Your team role cannot perform this action")
    return row[0]


def consume_quota(db, bucket, limit, minutes=60):
    window_seconds = minutes * 60
    now = int(time())
    start = now - now % window_seconds
    result = db.execute(
        """INSERT INTO usage_windows (bucket, window_start, hits) VALUES (?, ?, 1)
           ON CONFLICT (bucket, window_start) DO UPDATE SET hits=usage_windows.hits+1
           WHERE usage_windows.hits < ? RETURNING hits""",
        (sha256(bucket.encode()).hexdigest(), start, limit),
    ).fetchone()
    if not result:
        raise HTTPException(429, "Usage limit reached. Try again after the current window.",
                            headers={"Retry-After": str(start + window_seconds - now)})
    db.execute("DELETE FROM usage_windows WHERE window_start<?", (now - 2 * 86400,))


def register_teams(app, csrf, mapper):
    mutation = [Depends(csrf)]

    @app.get("/workspace/organizations")
    def organizations(request: Request):
        account = signed_in(request)
        with work.database() as db:
            rows = db.execute("""SELECT o.id, o.name, m.role FROM organizations o
                JOIN memberships m ON o.id=m.org_id WHERE m.account=? ORDER BY o.name, o.id""", (account,))
            return [{"id": row[0], "name": row[1], "role": row[2]} for row in rows]

    @app.post("/workspace/organizations", status_code=201, dependencies=mutation)
    def create(request: Request, body: OrganizationInput):
        account = signed_in(request)
        if os.getenv("CREDIT_PILOT_INVITE_ONLY") == "1":
            allowed = {email_digest(address) for address in os.getenv("CREDIT_PILOT_ADMINS", "").split(",")
                       if address.strip()}
            if request.session.get("verified_email") not in allowed:
                raise HTTPException(403, "Only designated pilot administrators can create teams")
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Enter a team name")
        org_id = uuid4().hex
        with work.database(write=True) as db:
            consume_quota(db, "organization:" + account, 5, 1440)
            db.execute("INSERT INTO organizations VALUES (?, ?, ?)", (org_id, name, work.now()))
            db.execute("INSERT INTO memberships VALUES (?, ?, 'admin', 'Administrator')", (org_id, account))
        audit("organization_created")
        return {"id": org_id, "name": name, "role": "admin"}

    @app.get("/workspace/organizations/{org_id}/reviewers")
    def reviewers(org_id: str, request: Request):
        account = signed_in(request)
        with work.database() as db:
            membership(db, account, org_id)
            return [{"id": row[0], "label": row[1]} for row in db.execute(
                "SELECT account, display_name FROM memberships WHERE org_id=? AND role='reviewer' ORDER BY display_name, account", (org_id,))]

    @app.get("/workspace/organizations/{org_id}/members")
    def members(org_id: str, request: Request):
        account = signed_in(request)
        with work.database() as db:
            membership(db, account, org_id, {"admin"})
            return [{"id": row[0], "name": row[1], "role": row[2]} for row in db.execute(
                "SELECT account, display_name, role FROM memberships WHERE org_id=? ORDER BY role, display_name", (org_id,))]

    @app.post("/workspace/organizations/{org_id}/members/{member_id}/revoke", dependencies=mutation)
    def revoke(org_id: str, member_id: str, request: Request):
        account = signed_in(request)
        with work.database(write=True) as db:
            membership(db, account, org_id, {"admin"})
            result = db.execute("""DELETE FROM memberships WHERE org_id=? AND account=?
                AND role!='admin' RETURNING account""", (org_id, member_id)).fetchone()
            if not result:
                raise HTTPException(404, "Member unavailable")
        audit("organization_member_revoked")
        return {"revoked": True}

    @app.get("/workspace/organizations/{org_id}/intake-summary")
    def intake_summary(org_id: str, request: Request):
        account = signed_in(request)
        with work.database() as db:
            membership(db, account, org_id, {"admin"})
            result = db.execute("""SELECT status, COUNT(*) FROM intake_jobs
                WHERE org_id=? AND deadline>? GROUP BY status""", (org_id, int(time()) - 7 * 86400))
            return {"counts": dict(result), "window_days": 7}

    @app.post("/workspace/organizations/{org_id}/invitations", status_code=201, dependencies=mutation)
    def invite(org_id: str, request: Request, body: InviteInput):
        account = signed_in(request)
        secret = token_urlsafe(32)
        if not body.name.strip():
            raise HTTPException(422, "Enter the invitee’s name")
        with work.database(write=True) as db:
            membership(db, account, org_id, {"admin"})
            consume_quota(db, "invite:" + org_id, 25, 1440)
            db.execute("INSERT INTO invitations VALUES (?, ?, ?, ?, ?, ?, NULL)",
                       (sha256(secret.encode()).hexdigest(), org_id, email_digest(body.email), body.role,
                        body.name.strip(), int(time()) + 2 * 86400))
        audit("organization_invitation_created")
        return {"path": "/#invite=" + secret, "expires_in_hours": 48}

    @app.post("/workspace/invitations/accept", dependencies=mutation)
    def accept(request: Request, body: AcceptInput):
        account = signed_in(request)
        verified = request.session.get("verified_email")
        if not verified:
            raise HTTPException(403, "Use a verified email account to accept this invitation")
        digest = sha256(body.token.encode()).hexdigest()
        with work.database(write=True) as db:
            row = db.execute("""UPDATE invitations SET used_at=? WHERE digest=? AND email_digest=?
                AND expires_at>? AND used_at IS NULL RETURNING org_id, role, display_name""",
                (work.now(), digest, verified, int(time()))).fetchone()
            if not row:
                raise HTTPException(404, "Invitation unavailable for this account")
            db.execute("""INSERT INTO memberships VALUES (?, ?, ?, ?) ON CONFLICT(org_id, account) DO NOTHING""",
                       (row[0], account, row[1], row[2]))
        audit("organization_invitation_accepted")
        return {"organization": row[0]}

    @app.post("/workspace/{case_id}/workflow", dependencies=mutation)
    def review(case_id: str, request: Request, body: ReviewAction):
        account = signed_in(request)
        with work.database(write=True) as db:
            state = work.load_case(db, account, case_id, lock=True)
            scope = state["access"]
            if not scope["organization"]:
                raise HTTPException(404, "Team case unavailable")
            work.check_revision(state, body.revision)
            if state.get("archived"):
                raise HTTPException(409, "Restore this archived case first")
            current = state.get("workflow", {"status": "draft"})
            analysis = work.result(state, mapper)
            if body.action == "submit":
                if scope["role"] not in {"admin", "analyst"} or current["status"] not in {"draft", "changes_requested"}:
                    raise HTTPException(403, "Only an analyst or administrator can submit an editable case")
                if analysis["provisional"]:
                    raise HTTPException(409, "Review all source rows and clear outstanding exceptions before submitting")
                if not body.reviewer:
                    raise HTTPException(422, "Choose an independent reviewer")
                membership(db, body.reviewer, scope["organization"], {"reviewer"})
                if body.reviewer == account:
                    raise HTTPException(422, "Choose an independent reviewer")
                next_state = {"status": "in_review", "submitted_by": account, "reviewer": body.reviewer,
                              "decline_percent": body.decline_percent, "notes": body.notes}
            elif body.action == "recall":
                if current["status"] != "in_review" or (scope["role"] != "admin" and current["submitted_by"] != account):
                    raise HTTPException(403, "Only the submitter or an administrator can recall this review")
                if not body.notes.strip():
                    raise HTTPException(422, "Explain why the review is being recalled")
                next_state = {"status": "draft"}
            else:
                if scope["role"] != "reviewer" or current["status"] != "in_review" or current["reviewer"] != account:
                    raise HTTPException(403, "Only the assigned reviewer can act on this submission")
                if body.action == "request_changes" and not body.notes.strip():
                    raise HTTPException(422, "Explain which changes the analyst should make")
                if body.action == "sign_off" and analysis["provisional"]:
                    raise HTTPException(409, "Outstanding exceptions must be resolved before sign-off")
                next_state = {**current, "status": "signed_off" if body.action == "sign_off" else "changes_requested"}
            at = work.now()
            state["workflow"] = next_state
            state["revision"] += 1
            state["history"].append({"action": body.action, "actor": account, "at": at,
                                     "revision": state["revision"], "reason": body.notes})
            if body.action == "sign_off":
                reviewer_name = db.execute("SELECT display_name FROM memberships WHERE org_id=? AND account=?",
                                           (scope["organization"], account)).fetchone()[0]
                entry = {"id": uuid4().hex, "at": at, "revision": state["revision"], "status": "signed_off"}
                result = {**entry, "name": state["name"], "document": state["document"],
                          "statement_versions": state["versions"], "analysis": analysis,
                          "history": state["history"], "notes": current["notes"],
                          "sign_off": {"reviewer": account, "submitted_by": current["submitted_by"],
                                       "notes": body.notes, "name": reviewer_name, "input_revision": body.revision, "at": at},
                          "scenario": cash_scenario(analysis, current["decline_percent"])}
                db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (case_id, entry["id"], json.dumps(result)))
                state["snapshots"].append(entry)
            work.store_case(db, case_id, state)
            response = work.view(work.load_case(db, account, case_id), mapper)
        audit("team_review_" + body.action)
        return response
