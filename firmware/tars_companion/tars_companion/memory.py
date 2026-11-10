from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_ -]?key|token|password|密码)\s*[:=：]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
]


def redact(text: str, limit: int | None = 1000) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text if limit is None else text[:limit]


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_ms INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            command_id TEXT,
            terminal_type TEXT
        )""")
        self._db.commit()

    def append(self, role: str, content: str, command_id: str | None = None,
               terminal_type: str | None = None) -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid memory role")
        with self._lock:
            self._db.execute("INSERT INTO events(created_at_ms,role,content,command_id,terminal_type) VALUES(?,?,?,?,?)",
                             (int(time.time() * 1000), role, redact(content), command_id, terminal_type))
            self._db.commit()

    def recent(self, limit: int = 8) -> list[tuple[str, str]]:
        limit = max(1, min(limit, 20))
        with self._lock:
            rows = self._db.execute("SELECT role,content FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return list(reversed(rows))

    def close(self) -> None:
        with self._lock:
            self._db.close()
