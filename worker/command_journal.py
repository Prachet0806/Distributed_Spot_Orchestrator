"""Durable worker command receipts and request binding across restarts."""
import json
import sqlite3
from pathlib import Path


class CommandJournal:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS commands (operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT)")

    def begin(self, command):
        request = json.dumps(command, sort_keys=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request,result FROM commands WHERE operation_id=?", (command["operation_id"],)).fetchone()
            if row:
                if row[0] != request:
                    raise RuntimeError("command operation identity conflict")
                if row[1] is None:
                    raise RuntimeError("command outcome UNKNOWN; probe before reissue")
                return json.loads(row[1])
            db.execute("INSERT INTO commands VALUES (?, ?, NULL)", (command["operation_id"], request))
        return None

    def finish(self, command, result):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("UPDATE commands SET result=? WHERE operation_id=?", (json.dumps(result), command["operation_id"]))
