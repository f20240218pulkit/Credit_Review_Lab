-- Apply after deploying the pilot code and provisioning a non-owner runtime role.
BEGIN;
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE cases FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS case_tenant_boundary ON cases;
CREATE POLICY case_tenant_boundary ON cases USING (
    (
        owner = nullif(current_setting('credit.case_actor', true), '')
        AND NOT EXISTS (SELECT 1 FROM team_cases tc WHERE tc.case_id = cases.id)
    ) OR EXISTS (
        SELECT 1 FROM team_cases tc
        JOIN memberships m ON m.org_id = tc.org_id
        WHERE tc.case_id = cases.id AND m.account = current_setting('credit.case_actor', true)
    )
);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS document_case_boundary ON documents;
CREATE POLICY document_case_boundary ON documents USING (
    EXISTS (SELECT 1 FROM cases WHERE cases.id = documents.case_id)
);

ALTER TABLE original_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE original_uploads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS original_case_boundary ON original_uploads;
CREATE POLICY original_case_boundary ON original_uploads USING (
    EXISTS (SELECT 1 FROM cases WHERE cases.id = original_uploads.case_id)
);

ALTER TABLE object_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE object_sources FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS object_case_boundary ON object_sources;
CREATE POLICY object_case_boundary ON object_sources USING (
    EXISTS (SELECT 1 FROM cases WHERE cases.id = object_sources.case_id)
);

ALTER TABLE snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE snapshots FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS snapshot_case_boundary ON snapshots;
CREATE POLICY snapshot_case_boundary ON snapshots USING (
    EXISTS (SELECT 1 FROM cases WHERE cases.id = snapshots.case_id)
);
COMMIT;
