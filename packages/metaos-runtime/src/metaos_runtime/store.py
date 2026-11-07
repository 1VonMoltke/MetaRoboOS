"""Persistent command identities and monotonic fencing epochs; no automatic replay."""

import json
import sqlite3
import time
from pathlib import Path
from threading import RLock


class Store:
    def __init__(self, workspace):
        self.root = Path(workspace)
        self.lock = RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS epochs (resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS faults (resource TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        # P0 does not recover a physical execution after gateway loss.
        for command_id, raw in self.db.execute("SELECT id,record FROM commands").fetchall():
            record = json.loads(raw)
            if record["status"] not in {"succeeded", "failed", "cancelled", "outcome_unknown"}:
                record.update(status="outcome_unknown", reason="gateway_restarted_requires_reconciliation")
                self.db.execute("UPDATE commands SET record=? WHERE id=?", (json.dumps(record), command_id))
                self.db.execute("INSERT OR REPLACE INTO faults VALUES (?,?)", (record["resource_id"], record["reason"]))
        self.db.commit()

    def epoch(self, resource):
        with self.lock, self.db:
            row = self.db.execute("SELECT epoch FROM epochs WHERE resource=?", (resource,)).fetchone()
            value = row[0] + 1 if row else 1
            self.db.execute("INSERT OR REPLACE INTO epochs VALUES (?,?)", (resource, value))
            return value

    def lookup(self, command_id):
        with self.lock:
            row = self.db.execute("SELECT owner,digest,record FROM commands WHERE id=?", (command_id,)).fetchone()
            return (row[0], row[1], json.loads(row[2])) if row else None

    def save(self, record, digest):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO commands VALUES (?,?,?,?)",
                (record["command_id"], record["owner"], digest, json.dumps(record, allow_nan=False)))

    def event(self, kind, **values):
        with self.lock:
            with (self.root / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"event": kind, "recorded_at_ns": time.monotonic_ns(), **values}, allow_nan=False) + "\n")

    def records(self, owner=None):
        with self.lock:
            rows = (self.db.execute("SELECT record FROM commands ORDER BY rowid").fetchall() if owner is None else
                    self.db.execute("SELECT record FROM commands WHERE owner=? ORDER BY rowid", (owner,)).fetchall())
            return [json.loads(row[0]) for row in rows]

    def faults(self):
        with self.lock:
            return dict(self.db.execute("SELECT resource,reason FROM faults").fetchall())

    def fault(self, resource, reason=None):
        with self.lock, self.db:
            if reason is None:
                self.db.execute("DELETE FROM faults WHERE resource=?", (resource,))
            else:
                self.db.execute("INSERT OR REPLACE INTO faults VALUES (?,?)", (resource, reason))

    def close(self):
        with self.lock:
            self.db.close()
