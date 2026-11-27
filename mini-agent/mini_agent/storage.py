"""Local session state and transaction boundaries."""

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                summarized_through INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                input TEXT NOT NULL,
                answer TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                reported INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, status, id);
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                title TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS todos_session ON todos(session_id, id);
            CREATE TABLE IF NOT EXISTS tool_events (
                run_id INTEGER NOT NULL REFERENCES runs(id),
                position INTEGER NOT NULL,
                call_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                ok INTEGER NOT NULL,
                payload TEXT NOT NULL,
                effect TEXT,
                PRIMARY KEY (run_id, position),
                UNIQUE (run_id, call_id)
            );
        """)

    def close(self):
        self.db.close()

    def create_session(self, name: str) -> int:
        with self.db:
            return self.db.execute("INSERT INTO sessions(name) VALUES (?)", (name,)).lastrowid

    def list_sessions(self):
        return self.db.execute("SELECT id, name FROM sessions ORDER BY id").fetchall()

    def session(self, session_id: int):
        return self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def start_run(self, session_id: int, text: str) -> int:
        with self.db:
            return self.db.execute(
                "INSERT INTO runs(session_id, input) VALUES (?, ?)", (session_id, text)
            ).lastrowid

    def finish_run(self, run_id: int, status: str, answer: str | None = None):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status = ?, answer = ? WHERE id = ?", (status, answer, run_id)
            )

    def recover_interrupted(self):
        with self.db:
            self.db.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running'")

    def pending_interruptions(self, session_id: int):
        return self.db.execute(
            "SELECT id FROM runs WHERE session_id = ? AND status = 'interrupted' "
            "AND reported = 0 ORDER BY id",
            (session_id,),
        ).fetchall()

    def acknowledge_interruption(self, run_id: int):
        with self.db:
            self.db.execute("UPDATE runs SET reported = 1 WHERE id = ?", (run_id,))

    def completed_turns(self, session_id: int, after: int):
        rows = self.db.execute(
            "SELECT id, input, answer FROM runs "
            "WHERE session_id = ? AND status = 'completed' AND id > ? ORDER BY id",
            (session_id, after),
        ).fetchall()
        return [(row, self.tool_results(row["id"])) for row in rows]

    def tool_results(self, run_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT tool, payload FROM tool_events WHERE run_id = ? AND ok = 1 ORDER BY position",
            (run_id,),
        )
        return [{"tool": row["tool"], "result": json.loads(row["payload"])} for row in rows]

    def update_summary(self, session_id: int, summary: str, through: int):
        with self.db:
            self.db.execute(
                "UPDATE sessions SET summary = ?, summarized_through = ? WHERE id = ?",
                (summary, through, session_id),
            )

    def add_todo(self, session_id: int, title: str) -> dict:
        todo_id = self.db.execute(
            "INSERT INTO todos(session_id, title) VALUES (?, ?)", (session_id, title)
        ).lastrowid
        return {"id": todo_id, "title": title, "done": False}

    def list_todos(self, session_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, title, done FROM todos WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        return [self._todo(row) for row in rows]

    def complete_todo(self, session_id: int, todo_id: int) -> tuple[dict | None, bool]:
        row = self.db.execute(
            "SELECT id, title, done FROM todos WHERE session_id = ? AND id = ?",
            (session_id, todo_id),
        ).fetchone()
        if row is None:
            return None, False
        todo = self._todo(row)
        changed = not todo["done"]
        if changed:
            self.db.execute(
                "UPDATE todos SET done = 1 WHERE session_id = ? AND id = ?", (session_id, todo_id)
            )
            todo["done"] = True
        return todo, changed

    @staticmethod
    def _todo(row) -> dict:
        return {"id": row["id"], "title": row["title"], "done": bool(row["done"])}

    def record_tool(
        self, run_id: int, position: int, call_id: str, tool: str,
        elapsed_ms: int, ok: bool, payload: dict, effect: str | None,
    ):
        # The caller commits the event together with any mutation.
        self.db.execute(
            "INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, position, call_id, tool, elapsed_ms, ok,
                json.dumps(payload, ensure_ascii=False), effect,
            ),
        )

    def effects(self, run_id: int) -> list[str]:
        rows = self.db.execute(
            "SELECT effect FROM tool_events WHERE run_id = ? AND effect IS NOT NULL ORDER BY position",
            (run_id,),
        )
        return [row["effect"] for row in rows]
