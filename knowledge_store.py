"""
Knowledge Store (knowledge_store.py)
Manages the SQLite database (`circuitproof_notebook.db`) for component facts and historical mistakes.
"""

import sqlite3
import json

class KnowledgeStore:
    def __init__(self, db_name="circuitproof_notebook.db"):
        self.conn = sqlite3.connect(db_name)
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS components (
                component_id TEXT PRIMARY KEY,
                category TEXT,
                facts_json TEXT,
                verified INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS mistakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                violation_text TEXT,
                fix_text TEXT,
                occurrence_count INTEGER
            )
        ''')
        self.conn.commit()

    def save_component(self, component_id, category, facts_dict, verified=1):
        cursor = self.conn.cursor()
        facts_str = json.dumps(facts_dict)
        cursor.execute('''
            INSERT OR REPLACE INTO components (component_id, category, facts_json, verified)
            VALUES (?, ?, ?, ?)
        ''', (component_id, category, facts_str, verified))
        self.conn.commit()
        print(f"[KnowledgeStore]: Saved component '{component_id}' (Verified: {verified})")

    def get_component(self, component_id):
        """Retrieves and decodes component facts from SQLite."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT facts_json FROM components WHERE component_id = ?", (component_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None

    def log_mistake(self, violation_text, fix_text):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, occurrence_count FROM mistakes WHERE violation_text = ?", (violation_text,))
        row = cursor.fetchone()
        if row:
            new_count = row[1] + 1
            cursor.execute("UPDATE mistakes SET occurrence_count = ? WHERE id = ?", (new_count, row[0]))
        else:
            cursor.execute("INSERT INTO mistakes (violation_text, fix_text, occurrence_count) VALUES (?, ?, 1)", (violation_text, fix_text))
        self.conn.commit()

    def close(self):
        self.conn.close()