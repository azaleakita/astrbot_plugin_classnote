"""Small SQLite store. Each operation is transactional; no awaits inside a transaction."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4


def migrate_legacy_database(data_root):
    """Copy a legacy snapshot once. Never modify or delete the original database."""
    root = Path(data_root) / "plugin_data"
    target = root / "astrbot_plugin_classnote" / "timetables.sqlite3"
    legacy = root / "astrbot_plugin_wakeup_reminder" / "timetables.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or not legacy.is_file():
        return target
    temporary = target.with_suffix(".migrating")
    try:
        with sqlite3.connect(f"file:{legacy}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bindings (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL, updated REAL NOT NULL, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS delivery_binding ON deliveries(binding_id);
        """)

    def all(self):
        return [
            json.loads(row[0]) for row in self.db.execute("SELECT body FROM bindings")
        ]

    def get(self, binding_id):
        row = self.db.execute(
            "SELECT body FROM bindings WHERE id=?", (binding_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, binding):
        previous = self.get(binding["id"])
        binding = {
            **binding,
            "revision": (previous or {}).get("revision", 0) + 1,
            "epoch": (previous or {}).get("epoch") or uuid4().hex,
        }
        with self.db:
            self.db.execute(
                "INSERT INTO bindings VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE "
                "SET revision=excluded.revision, body=excluded.body",
                (
                    binding["id"],
                    binding["revision"],
                    json.dumps(binding, ensure_ascii=False),
                ),
            )
        return binding

    def delete(self, binding_id):
        with self.db:
            self.db.execute("DELETE FROM bindings WHERE id=?", (binding_id,))
            self.db.execute("DELETE FROM deliveries WHERE binding_id=?", (binding_id,))

    def claim(self, key, binding_id, now):
        """Retry transient failures, at most three attempts and no duplicate live claim."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, 'pending', 0, 0, '')",
                (key, binding_id),
            )
            cursor = self.db.execute(
                "UPDATE deliveries SET state='sending', attempts=attempts+1, updated=? "
                "WHERE id=? AND attempts<3 AND "
                "(state='pending' OR (state='failed' AND updated<=?) "
                "OR (state='sending' AND updated<=?))",
                (now, key, now - 30, now - 120),
            )
        return cursor.rowcount == 1

    def finish(self, key, now, state="sent", error=""):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state=?, updated=?, error=? WHERE id=?",
                (state, now, error[:100], key),
            )

    def latest(self, binding_id):
        return self.db.execute(
            "SELECT state, attempts, error FROM deliveries WHERE binding_id=? "
            "ORDER BY updated DESC LIMIT 1",
            (binding_id,),
        ).fetchone()

    def prune(self, now):
        with self.db:
            self.db.execute(
                "DELETE FROM deliveries WHERE updated < ?", (now - 90 * 86400,)
            )

    def close(self):
        self.db.close()
