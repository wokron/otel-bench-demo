"""Durable action intents, idempotency, and deadline reconciliation."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from demo_api.models import APIError


class Experiments:
    def __init__(self, path, targets):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.targets = targets
        self.db.execute("""CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, target TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            document TEXT NOT NULL, UNIQUE(target, key))""")
        self.db.commit()

    def save(self, doc):
        self.db.execute("UPDATE experiments SET document=? WHERE id=?", (json.dumps(doc), doc["id"]))
        self.db.commit()
        return doc

    def get(self, target, experiment_id):
        row = self.db.execute("SELECT document FROM experiments WHERE id=? AND target=?",
                              (experiment_id, target)).fetchone()
        if row is None:
            raise APIError("experiment_not_found", "Unknown experiment for this target", 404)
        return json.loads(row[0])

    def list(self, target):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT document FROM experiments WHERE target=? ORDER BY rowid DESC LIMIT 100", (target,))]

    def create(self, target, request, key):
        actuator = self.targets[target].actuator
        if actuator is None:
            raise APIError("unsupported_action", "Actions are not configured", 422)
        if request.dry_run:
            return {"dry_run": True, "plan": actuator.plan(request)}
        if not key or len(key) > 200:
            raise APIError("idempotency_key_required", "Execution requires Idempotency-Key (1..200 characters)", 422)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.lock:
            existing = self.db.execute("SELECT fingerprint, document FROM experiments WHERE target=? AND key=?",
                                       (target, key)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise APIError("idempotency_conflict", "Key already used with a different request", 409)
                return json.loads(existing[1])
            plan = actuator.plan(request)
            # Serialize one active experiment per target to avoid overlapping faults.
            rows = self.db.execute("SELECT document FROM experiments WHERE target=?", (target,))
            if any(json.loads(r[0])["state"] not in {"cancelled", "completed"} for r in rows):
                raise APIError("experiment_conflict", "Cancel/recover the previous experiment first", 409)
            experiment_id = "da-" + uuid.uuid4().hex
            now = time.time()
            doc = {"id": experiment_id, "target_id": target, "action": request.action,
                   "state": "creating", "created_at": now, "expires_at": now + request.duration_seconds,
                   "plan": plan, "handle": {"name": experiment_id}}
            self.db.execute("INSERT INTO experiments VALUES (?,?,?,?,?)",
                            (experiment_id, target, key, fingerprint, json.dumps(doc)))
            self.db.commit()  # Intent exists even if process dies during the backend write.
            try:
                doc["handle"] = actuator.apply(experiment_id, plan)
                doc["state"] = "pending"
            except Exception as exc:
                # Transport failures can occur after successful create. Never claim nothing happened.
                doc["state"] = "unknown"
                doc["last_error"] = str(exc)
                self.save(doc)
                raise APIError("action_outcome_unknown", f"Inspect/cancel experiment {experiment_id}: {exc}") from exc
            return self.save(doc)

    def refresh(self, target, experiment_id):
        with self.lock:
            doc = self.get(target, experiment_id)
            if doc["state"] in {"cancelled", "completed"}:
                return doc
            actuator = self.targets[target].actuator
            if actuator is None:
                raise APIError("backend_unavailable", "Actuator removed from config; restore it to recover experiments")
            if time.time() >= doc["expires_at"] or doc["state"] == "cancelling":
                doc.update(actuator.cancel(experiment_id, doc["handle"]))
            else:
                status = actuator.status(experiment_id, doc["handle"])
                if status["state"] == "absent":
                    status["state"] = "cancelled"
                doc.update(status)
            doc.pop("last_error", None)
            return self.save(doc)

    def cancel(self, target, experiment_id):
        with self.lock:
            doc = self.get(target, experiment_id)
            if doc["state"] in {"cancelled", "completed"}:
                return doc
            # Persist cancellation before making the backend call, so retries survive a restart.
            doc["state"] = "cancelling"
            self.save(doc)
            return self.refresh(target, experiment_id)

    def reconcile(self):
        with self.lock:
            # No LIMIT: all durable intents must receive recovery, including older ones.
            docs = [json.loads(r[0]) for r in self.db.execute("SELECT document FROM experiments")]
            for doc in docs:
                if doc["state"] in {"cancelled", "completed"}:
                    continue
                try:
                    self.refresh(doc["target_id"], doc["id"])
                except Exception as exc:
                    doc["last_error"] = str(exc)
                    self.save(doc)
