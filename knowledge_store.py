"""
Knowledge Store (knowledge_store.py)

SQLite store for component facts, KiCad symbol mappings, scanned parts and past mistakes.

Facts format (one JSON object per part; every field optional):
{
  "vdd_min": 2.0, "vdd_max": 3.6,            # allowed supply range (V)
  "supply_pins": ["VDD"],                     # pin names that must sit on a rail in range
  "io": {"vih_min": 2.0, "vil_max": 0.8,      # default I/O thresholds (V)
         "voh_min": 2.4, "vol_max": 0.4,
         "vin_max": 4.0},                     # max voltage an input pin tolerates
  "pins": {"PB7": {"functions": ["I2C1_SDA"], "vin_max": 5.5}},   # per-pin overrides
  "spi_max_hz": 18000000
}
'verified' means a person checked the numbers against the datasheet. Unverified facts are
still used, but every finding based on them says so.
"""

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    part_id    TEXT PRIMARY KEY,
    category   TEXT,
    facts_json TEXT NOT NULL,
    verified   INTEGER NOT NULL DEFAULT 0,
    source     TEXT
);
CREATE TABLE IF NOT EXISTS lib_map (
    lib_id  TEXT PRIMARY KEY,
    part_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_parts (
    lib_id     TEXT,
    value      TEXT,
    project    TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (lib_id, value, project)
);
CREATE TABLE IF NOT EXISTS mistakes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    violation_text   TEXT,
    fix_text         TEXT,
    occurrence_count INTEGER
);
"""


class KnowledgeStore:
    """Opens a short-lived connection per call, so it is safe under FastAPI's thread pool."""

    def __init__(self, db_path="circuitproof_notebook.db"):
        self.db_path = db_path
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _run(self, sql, args=(), fetch=False):
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    # ---- parts
    def save_part(self, part_id, category, facts, verified=False, source=""):
        self._run("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)",
                  (part_id, category, json.dumps(facts), int(bool(verified)), source))

    def get_part(self, part_id):
        rows = self._run("SELECT part_id, category, facts_json, verified, source FROM parts "
                         "WHERE part_id = ? COLLATE NOCASE", (part_id,), fetch=True)
        if not rows:
            return None
        pid, cat, facts, verified, source = rows[0]
        return {"part_id": pid, "category": cat, "facts": json.loads(facts),
                "verified": bool(verified), "source": source or ""}

    # ---- KiCad symbol <-> part mapping
    def map_lib(self, lib_id, part_id):
        self._run("INSERT OR REPLACE INTO lib_map VALUES (?, ?)", (lib_id, part_id))

    def resolve(self, lib_id="", value="", part_id=""):
        """Find facts for a component: explicit part_id, then symbol mapping, then value."""
        for candidate in (part_id, self._mapped(lib_id), value):
            if candidate:
                part = self.get_part(candidate)
                if part:
                    return part
        return None

    def _mapped(self, lib_id):
        if not lib_id:
            return ""
        rows = self._run("SELECT part_id FROM lib_map WHERE lib_id = ?", (lib_id,), fetch=True)
        return rows[0][0] if rows else ""

    def catalog(self):
        rows = self._run("SELECT m.lib_id, m.part_id, p.category, p.verified FROM lib_map m "
                         "LEFT JOIN parts p ON p.part_id = m.part_id ORDER BY m.lib_id", fetch=True)
        return [{"lib_id": r[0], "part_id": r[1], "category": r[2] or "", "verified": bool(r[3])}
                for r in rows]

    # ---- scanner bookkeeping (never touches verified facts)
    def record_observed(self, lib_id, value, project):
        self._run("INSERT INTO observed_parts VALUES (?, ?, ?, 1) ON CONFLICT(lib_id, value, project) "
                  "DO UPDATE SET seen_count = seen_count + 1", (lib_id, value or "", project or ""))

    # ---- mistakes
    def log_mistake(self, violation_text, fix_text):
        rows = self._run("SELECT id, occurrence_count FROM mistakes WHERE violation_text = ?",
                         (violation_text,), fetch=True)
        if rows:
            self._run("UPDATE mistakes SET occurrence_count = ? WHERE id = ?",
                      ((rows[0][1] or 0) + 1, rows[0][0]))
        else:
            self._run("INSERT INTO mistakes (violation_text, fix_text, occurrence_count) "
                      "VALUES (?, ?, 1)", (violation_text, fix_text))

    def close(self):
        """Kept for compatibility; connections are already closed after each call."""
