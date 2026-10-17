-- 001_initial.sql: Schemas, core tables, constraints, and role permissions for IncidentLab

CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS sandbox;
CREATE SCHEMA IF NOT EXISTS checkpoints;

-- Schema migration ledger
CREATE TABLE IF NOT EXISTS control.schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Incidents table with single active incident constraint
CREATE TABLE IF NOT EXISTS control.incidents (
    id UUID PRIMARY KEY,
    state TEXT NOT NULL,
    symptom TEXT NOT NULL,
    request_id UUID NOT NULL,
    order_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active_ms INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    outcome JSONB
);

-- Enforce at most one nonterminal active incident in the control database
CREATE UNIQUE INDEX IF NOT EXISTS ix_one_active_incident ON control.incidents ((TRUE))
WHERE state NOT IN (
    'verified_recovered',
    'verification_failed',
    'escalated',
    'failed',
    'rejected',
    'expired',
    'cancelled',
    'closed_unknown',
    'stale'
);

-- Evidence collection
CREATE TABLE IF NOT EXISTS control.evidence (
    incident_id UUID NOT NULL REFERENCES control.incidents(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    section_id TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data JSONB NOT NULL,
    PRIMARY KEY (incident_id, evidence_id)
);

-- Immutable repair proposals
CREATE TABLE IF NOT EXISTS control.proposals (
    id UUID PRIMARY KEY,
    incident_id UUID NOT NULL REFERENCES control.incidents(id) ON DELETE CASCADE,
    payload JSONB NOT NULL,
    payload_digest TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    evidence_ids JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- At most one live proposal per incident
CREATE UNIQUE INDEX IF NOT EXISTS ix_one_live_proposal ON control.proposals (incident_id);

-- Bound human approvals
CREATE TABLE IF NOT EXISTS control.approvals (
    proposal_id UUID PRIMARY KEY REFERENCES control.proposals(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT 'owner',
    reviewed_digest TEXT NOT NULL,
    reviewed_revision INTEGER NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Execution claims and receipts
CREATE TABLE IF NOT EXISTS control.executions (
    execution_key UUID PRIMARY KEY,
    proposal_id UUID NOT NULL UNIQUE REFERENCES control.proposals(id) ON DELETE CASCADE,
    incident_id UUID NOT NULL REFERENCES control.incidents(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    receipt JSONB
);

-- Ordered event timeline
CREATE TABLE IF NOT EXISTS control.events (
    incident_id UUID NOT NULL REFERENCES control.incidents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL,
    duration_ms INTEGER,
    token_count INTEGER,
    PRIMARY KEY (incident_id, seq)
);

-- Versioned runbook chunks for full-text keyword retrieval
CREATE TABLE IF NOT EXISTS control.runbook_chunks (
    chunk_id TEXT PRIMARY KEY,
    runbook_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    section_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    tsv TSVECTOR NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runbook_chunks_tsv ON control.runbook_chunks USING GIN(tsv);

-- --- Sandbox tables ---

CREATE TABLE IF NOT EXISTS sandbox.target_state (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 1,
    endpoint_key TEXT NOT NULL DEFAULT 'inventory-local',
    worker_running BOOLEAN NOT NULL DEFAULT TRUE,
    worker_id TEXT NOT NULL DEFAULT 'checkout-worker',
    maintenance BOOLEAN NOT NULL DEFAULT FALSE
);

-- Initial target state row
INSERT INTO sandbox.target_state (id, revision, endpoint_key, worker_running, worker_id, maintenance)
VALUES (1, 1, 'inventory-local', TRUE, 'checkout-worker', FALSE)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS sandbox.orders (
    order_id UUID PRIMARY KEY,
    request_key UUID NOT NULL UNIQUE,
    request_id UUID NOT NULL,
    sku TEXT NOT NULL DEFAULT 'SKU-1',
    quantity INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fulfilled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sandbox.logs (
    seq BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id UUID,
    order_id UUID,
    event_kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    data JSONB
);

CREATE TABLE IF NOT EXISTS sandbox.repair_receipts (
    execution_key UUID PRIMARY KEY,
    payload_digest TEXT NOT NULL,
    result TEXT NOT NULL,
    before_revision INTEGER NOT NULL,
    after_revision INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- --- Role Permissions and Boundaries ---

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'incidentlab_control') THEN
        GRANT USAGE ON SCHEMA control, checkpoints TO incidentlab_control;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA control, checkpoints TO incidentlab_control;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA control, checkpoints TO incidentlab_control;
        ALTER DEFAULT PRIVILEGES IN SCHEMA control GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_control;
        ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_control;
    END IF;

    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'incidentlab_sandbox') THEN
        GRANT USAGE ON SCHEMA sandbox TO incidentlab_sandbox;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA sandbox TO incidentlab_sandbox;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA sandbox TO incidentlab_sandbox;
        ALTER DEFAULT PRIVILEGES IN SCHEMA sandbox GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_sandbox;
    END IF;
END
$$;
