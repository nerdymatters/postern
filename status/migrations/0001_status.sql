CREATE TABLE checks (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('operational', 'degraded', 'outage', 'unknown')),
  checked_at INTEGER NOT NULL
);
CREATE TABLE changes (
  id INTEGER PRIMARY KEY,
  service TEXT NOT NULL,
  state TEXT NOT NULL,
  changed_at INTEGER NOT NULL
);
CREATE INDEX changes_time ON changes(changed_at);
