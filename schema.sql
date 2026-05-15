-- House Knowledge Base Schema
-- Hybrid EAV: core universal columns + flexible attribute overflow
PRAGMA foreign_keys = ON;

-- Location hierarchy: Property > Zone > Area
-- e.g. Property > Exterior > Driveway
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Core items table
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT,
    location_id     INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    purchased_date  TEXT,
    installed_date  TEXT,
    manufacturer    TEXT,
    model           TEXT,
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- EAV overflow — arbitrary specs per item
CREATE TABLE IF NOT EXISTS attributes (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Events — history log per item
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event_date  TEXT,
    event_type  TEXT,   -- 'purchased', 'installed', 'repaired', 'replaced', 'inspected', 'noted'
    description TEXT,
    cost        REAL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Relationships between items
CREATE TABLE IF NOT EXISTS relationships (
    id          INTEGER PRIMARY KEY,
    from_item   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    to_item     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,  -- 'controls', 'feeds', 'connected_to', 'part_of', 'adjacent_to'
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Human users
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    is_admin        INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- API keys for agent access
CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,   -- SHA-256 hash of the actual key
    label       TEXT NOT NULL,          -- e.g. 'claude-agent', 'home-assistant'
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_used   TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- File attachments per item (images converted to JPEG, PDFs stored as-is)
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime_type     TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_items_location ON items(location_id);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_attributes_item ON attributes(item_id);
CREATE INDEX IF NOT EXISTS idx_attributes_key ON attributes(key);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id);
CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships(from_item);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_item);
CREATE INDEX IF NOT EXISTS idx_attachments_item ON attachments(item_id);
