"""SQLite transactions enforce idempotence and prevent overlapping reservations."""

from datetime import datetime, timedelta
import json
import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (
                operation_key TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                material_id TEXT NOT NULL, channel TEXT NOT NULL,
                proposed_at TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def register(self, request):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO requests VALUES (?, ?)",
                            (request.request_id, request.fingerprint()))
            row = self.db.execute("SELECT fingerprint FROM requests WHERE request_id=?",
                                  (request.request_id,)).fetchone()
            if row[0] != request.fingerprint():
                raise ValueError("request_id reused with different input; use a new request_id")

    def get(self, key):
        row = self.db.execute("SELECT payload FROM drafts WHERE operation_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def reservations(self, channel):
        return [dict(row) for row in self.db.execute(
            "SELECT operation_key, material_id, proposed_at FROM drafts WHERE channel=?", (channel,))]

    def save(self, payload, occupied, fail=False):
        """Recheck collisions inside BEGIN IMMEDIATE, including pending drafts.

        An existing exact payload is returned even during injected failures: no write
        is needed. Changed proposals for the same identity are rejected, except an
        exact pending -> confirmed upgrade carrying trusted user confirmation.
        """
        try:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.get(payload["operation_key"])
            if old:
                same = all(old.get(k) == payload.get(k) for k in payload
                           if k not in {"status", "confirmation"})
                if not same:
                    self.db.rollback()
                    return {"ok": False, "error": "idempotency_conflict",
                            "detail": "existing draft differs; no silent overwrite"}
                if old["status"] == payload["status"] or old["status"] == "ready":
                    self.db.commit()
                    return {"ok": True, "save_status": "already_exists", "draft": old}
                if not (old["status"] == "pending_confirmation" and payload["status"] == "ready"
                        and payload["confirmation"] == "user_confirmed_alternative"):
                    self.db.rollback()
                    return {"ok": False, "error": "invalid_draft_transition"}
            if fail:
                raise sqlite3.OperationalError("injected save failure")
            candidate = datetime.fromisoformat(payload["proposed_at"])
            slots = list(occupied) + self.reservations(payload["channel"])
            for slot in slots:
                if slot.get("operation_key") == payload["operation_key"]:
                    continue
                start = datetime.fromisoformat(slot["proposed_at"])
                if start < candidate + timedelta(hours=1) and candidate < start + timedelta(hours=1):
                    self.db.rollback()
                    return {"ok": False, "error": "slot_conflict_on_save"}
            self.db.execute("""INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_key) DO UPDATE SET status=excluded.status,
                payload=excluded.payload""", (
                payload["operation_key"], payload["draft_id"], payload["material_id"],
                payload["channel"], payload["proposed_at"], payload["status"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            self.db.commit()
            return {"ok": True, "save_status": "updated" if old else "saved", "draft": payload}
        except sqlite3.Error:
            self.db.rollback()
            return {"ok": False, "error": "save_failed", "detail": "draft write failed; nothing committed"}
