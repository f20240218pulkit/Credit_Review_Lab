"""Verify the deployment RLS migration against a disposable CI database."""
import os
from pathlib import Path

from psycopg import connect

from credit_review.api import CASES, parse_pdf
from credit_review.documents import make_demo_pdf
from credit_review.workspace import database, init_postgres, create_case, load_case

url = os.environ["CREDIT_DATABASE_URL"]
if os.environ.get("CREDIT_PILOT_TEST_RLS") != "1":
    raise RuntimeError("Only run this check on a disposable CI database")
init_postgres(url)

with connect(url, autocommit=True) as setup:
    setup.execute("CREATE ROLE credit_pilot_rls_test NOLOGIN NOSUPERUSER NOBYPASSRLS")
    setup.execute("GRANT USAGE ON SCHEMA public TO credit_pilot_rls_test")
    setup.execute("""GRANT SELECT, INSERT, UPDATE ON cases TO credit_pilot_rls_test;
        GRANT SELECT, INSERT ON team_cases, documents, snapshots TO credit_pilot_rls_test;
        GRANT SELECT ON memberships, original_uploads, object_sources TO credit_pilot_rls_test;
        GRANT USAGE ON SEQUENCE cases_rowid_seq TO credit_pilot_rls_test""")
    setup.execute(Path("migrations/002_pilot_case_rls.sql").read_text())
    setup.execute("INSERT INTO organizations VALUES ('rls-org', 'RLS test team', '2026-01-01')")
    setup.execute("INSERT INTO memberships VALUES ('rls-org', 'rls-analyst', 'analyst', 'Analyst')")
    setup.execute("INSERT INTO memberships VALUES ('rls-org', 'rls-reviewer', 'reviewer', 'Reviewer')")

pack = make_demo_pdf(CASES["CR-101"])
rows = parse_pdf(pack)

with database(write=True) as db:
    db.execute("SET LOCAL ROLE credit_pilot_rls_test")
    team = create_case(db, "rls-analyst", "Team file", pack, rows, organization="rls-org")
    personal = create_case(db, "private-account", "Personal file", pack, rows)
    assert load_case(db, "rls-reviewer", team["id"])["id"] == team["id"]
    assert db.execute("SELECT count(*) FROM documents WHERE case_id=?", (team["id"],)).fetchone() == (1,)
    db.execute("SELECT set_config('credit.case_actor', 'outsider', true)")
    assert db.execute("SELECT count(*) FROM cases WHERE id IN (?, ?)", (team["id"], personal["id"])).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM documents WHERE case_id IN (?, ?)",
                      (team["id"], personal["id"])).fetchone() == (0,)
    assert load_case(db, "private-account", personal["id"])["id"] == personal["id"]

with connect(url) as client:
    client.execute("SET LOCAL ROLE credit_pilot_rls_test")
    assert client.execute("SELECT count(*) FROM cases").fetchone() == (0,)

print("Case and evidence policies verified with a non-owner PostgreSQL role")
