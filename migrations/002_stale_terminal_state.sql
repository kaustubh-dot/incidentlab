-- Migration 002: Add 'stale' state to the terminal-state exclusion list for ix_one_active_incident
DROP INDEX IF EXISTS control.ix_one_active_incident;

CREATE UNIQUE INDEX ix_one_active_incident ON control.incidents ((TRUE))
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
