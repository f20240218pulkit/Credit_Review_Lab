"""Persistent, single-user workspaces isolated by a browser session capability."""
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi import HTTPException
from pydantic import TypeAdapter

from .engine import Review, SourceRow, analyse

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    case_id TEXT NOT NULL REFERENCES cases(id), id TEXT NOT NULL, pdf BLOB NOT NULL,
    PRIMARY KEY(case_id, id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    case_id TEXT NOT NULL REFERENCES cases(id), id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(case_id, id)
);
"""


def now():
    return datetime.now(UTC).isoformat()


def as_json(data):
    return TypeAdapter(dict).dump_python(data, mode="json")


@contextmanager
def database(write=False):
    url = os.environ.get("CREDIT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        with postgres_database(url, write) as db:
            yield db
        return
    if os.environ.get("VERCEL") == "1":
        raise HTTPException(503, "Set CREDIT_DATABASE_URL or DATABASE_URL to enable the Vercel workspace")
    path = Path(os.environ.get("CREDIT_DB", "data/credit-review.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        return self.connection.execute(query.replace("?", "%s"), params)


@lru_cache(maxsize=4)
def init_postgres(url):
    import psycopg

    schema = SCHEMA.replace("id TEXT PRIMARY KEY, owner",
                            "rowid BIGINT GENERATED ALWAYS AS IDENTITY, id TEXT PRIMARY KEY, owner")
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute("SELECT pg_advisory_xact_lock(62479301)")
        for statement in schema.replace("BLOB", "BYTEA").split(";"):
            if statement.strip():
                db.execute(statement)


@contextmanager
def postgres_database(url, write):
    import psycopg

    init_postgres(url)
    with psycopg.connect(url, connect_timeout=10) as db:
        if write:
            # SQLite serializes writes at the database level. Use the same
            # contract here so revision checks and writes are atomic.
            db.execute("SELECT pg_advisory_xact_lock(62479302)")
        yield PostgresConnection(db)


def load_case(db, owner, case_id):
    record = db.execute("SELECT state FROM cases WHERE id=? AND owner=?", (case_id, owner)).fetchone()
    if record is None:
        raise HTTPException(404, "Workspace case unavailable")
    return json.loads(record[0])


def store_case(db, case_id, state):
    db.execute("UPDATE cases SET state=? WHERE id=?", (json.dumps(state), case_id))


def check_revision(state, revision):
    if state["revision"] != revision:
        raise HTTPException(409, "The case changed. Reload the latest version and retry.")


def result(state, mapper):
    rows = [SourceRow.model_validate(row) for row in state["rows"]]
    reviews = [Review.model_validate(event["decision"]) for event in state["reviews"].values()]
    return as_json(analyse(rows, reviews, mapper, state["years"]))


def view(state, mapper):
    return {**{key: value for key, value in state.items() if key != "rows"},
            "analysis": result(state, mapper)}


def save_document(db, case_id, rows, pdf):
    doc_id = rows[0].document
    db.execute("INSERT INTO documents (case_id, id, pdf) VALUES (?, ?, ?) ON CONFLICT (case_id, id) DO NOTHING", (case_id, doc_id, pdf))
    return doc_id


def create_case(db, owner, name, pdf, rows, demo=None):
    years = sorted({row.year for row in rows})
    if len(years) != 2:
        raise HTTPException(422, "A workspace needs a statement pack with two reporting years")
    case_id = uuid4().hex
    db.execute("INSERT INTO cases (id, owner, state) VALUES (?, ?, ?)", (case_id, owner, "{}"))
    doc_id = save_document(db, case_id, rows, pdf)
    state = {"id": case_id, "name": name, "demo": demo, "years": years, "revision": 1,
             "document": doc_id, "versions": [{"document": doc_id, "at": now()}],
             "rows": [row.model_dump(mode="json") for row in rows],
             "reviews": {}, "history": [], "snapshots": [], "comparison": None}
    store_case(db, case_id, state)
    return state


def review_row(state, decision, mapper):
    row = next((row for row in state["rows"] if row["id"] == decision.row_id), None)
    if row is None:
        raise HTTPException(422, "Review must reference an active source row")
    previous = state["reviews"].get(decision.row_id)
    before = previous["decision"]["account"] if previous else mapper.suggest(row["label"])["account"]
    event = {"action": "review", "document": state["document"], "at": now(), "before": before,
             "decision": decision.model_dump(mode="json")}
    state["reviews"][decision.row_id] = event
    state["history"].append(event)
    state["revision"] += 1


def row_signature(row):
    return tuple(row[key] for key in ("year", "label", "value", "unit", "currency"))


def replace_document(db, state, pdf, rows, mapper):
    if {row.year for row in rows} != set(state["years"]):
        raise HTTPException(422, "Replacement reporting years must match the case")
    new_rows = [row.model_dump(mode="json") for row in rows]
    old_id = state["document"]
    new_id = rows[0].document
    if new_id == old_id:
        return state
    old_analysis = result(state, mapper)
    old_rows = state["rows"]
    carried = {}
    for new_row in new_rows:
        signature = row_signature(new_row)
        matches = [row for row in old_rows if row_signature(row) == signature]
        if len(matches) != 1 or sum(row_signature(row) == signature for row in new_rows) != 1:
            continue
        previous = state["reviews"].get(matches[0]["id"])
        # A label with a new competing row may need to be reconsidered.
        peers = lambda rows: sorted(row_signature(row) for row in rows if row["year"] == new_row["year"]
                                    and mapper.suggest(row["label"])["account"] ==
                                    mapper.suggest(new_row["label"])["account"])
        if previous and peers(old_rows) == peers(new_rows):
            event = {"action": "carried_review", "at": now(), "document": new_id,
                     "from_row": matches[0]["id"], "from_document": old_id,
                     "decision": {**previous["decision"], "row_id": new_row["id"]}}
            carried[new_row["id"]] = event

    save_document(db, state["id"], rows, pdf)
    reopened = len(state["reviews"]) - len(carried)
    state.update(document=new_id, rows=new_rows, reviews=carried, revision=state["revision"] + 1)
    state["versions"].append({"document": new_id, "at": now()})
    state["history"].append({"action": "replacement", "at": now(), "from_document": old_id,
                             "document": new_id, "reopened_reviews": reopened})
    state["history"].extend(carried.values())
    updated = result(state, mapper)
    differences = []
    for before, after in zip(old_analysis["annual"], updated["annual"], strict=True):
        for group in ("accounts", "metrics"):
            for key, old in before[group].items():
                new = after[group][key]
                fields = ("value", "reason") if group == "metrics" else ("value", "status")
                if any(old.get(field) != new.get(field) for field in fields):
                    differences.append({"year": after["year"], "name": key, "before": old, "after": new})
    state["comparison"] = {"from_document": old_id, "document": new_id, "reopened_reviews": reopened,
                           "differences": differences}
    store_case(db, state["id"], state)
    return state
