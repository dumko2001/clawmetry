-- ClawMetry Supabase Schema
-- Run this in your Supabase SQL editor to set up cloud persistence.

-- ── Nodes ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    node_id    TEXT PRIMARY KEY,
    name       TEXT,
    hostname   TEXT,
    version    TEXT,
    tags       JSONB DEFAULT '[]',
    status     TEXT DEFAULT 'online',
    registered_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ DEFAULT NOW(),
    metadata   JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes (last_seen_at);

-- ── Metrics ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    node_id     TEXT,
    metric_name TEXT,
    value       FLOAT8,
    attributes  JSONB DEFAULT '{}',
    ts          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics (ts);
CREATE INDEX IF NOT EXISTS idx_metrics_node_id ON metrics (node_id);

-- ── Events ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    node_id     TEXT,
    event_type  TEXT,
    session_id  TEXT,
    data        JSONB DEFAULT '{}',
    ts          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_node_id ON events (node_id);

-- ── Sessions ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    node_id      TEXT,
    session_id   TEXT,
    display_name TEXT,
    status       TEXT,
    model        TEXT,
    total_tokens INT DEFAULT 0,
    cost_usd     FLOAT8 DEFAULT 0,
    started_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_node_id ON sessions (node_id);

-- ── 7-day auto-cleanup ────────────────────────────────────────────────
-- To enable 7-day auto-cleanup, run in Supabase SQL editor:
-- SELECT cron.schedule('cleanup-old-data', '0 * * * *', $$DELETE FROM metrics WHERE ts < NOW() - INTERVAL '7 days'; DELETE FROM events WHERE ts < NOW() - INTERVAL '7 days'; DELETE FROM sessions WHERE updated_at < NOW() - INTERVAL '7 days';$$);
