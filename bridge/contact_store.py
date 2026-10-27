"""Relay-owned contact associations and Codex chat history, in native SQLite."""
import os
from pathlib import Path
import sqlite3
import threading
import time


class ContactStore:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, model TEXT, title TEXT, thread TEXT,
                    created INTEGER, updated INTEGER);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, sid TEXT, role TEXT, text TEXT,
                    speaker TEXT, created INTEGER);
                CREATE TABLE IF NOT EXISTS contacts (
                    peer TEXT, model TEXT, sid TEXT, PRIMARY KEY(peer,model));
                CREATE TABLE IF NOT EXISTS model_visibility (
                    peer TEXT, model TEXT, shown INTEGER, PRIMARY KEY(peer,model));
            """)

    def get(self, sid):
        with self.lock:
            row = self.db.execute("SELECT * FROM chats WHERE id=?", (sid,)).fetchone()
            return dict(row) if row else None

    def save(self, sid, model, title, thread=None):
        now = int(time.time() * 1000)
        with self.lock, self.db:
            self.db.execute("""INSERT INTO chats VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET model=excluded.model,title=excluded.title,
                thread=coalesce(excluded.thread,chats.thread),updated=excluded.updated""",
                (sid, model, title, thread, now, now))

    def remember(self, peer, model, sid):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?)", (peer, model, sid))

    def last(self, peer, model):
        with self.lock:
            row = self.db.execute("SELECT sid FROM contacts WHERE peer=? AND model=?", (peer, model)).fetchone()
            return row[0] if row else ""

    def visibility(self, peer):
        with self.lock:
            return {row["model"]: bool(row["shown"]) for row in
                    self.db.execute("SELECT model,shown FROM model_visibility WHERE peer=?", (peer,))}

    def set_visibility(self, peer, choices, reset=False):
        with self.lock, self.db:
            if reset: self.db.execute("DELETE FROM model_visibility WHERE peer=?", (peer,))
            for model, shown in choices.items():
                self.db.execute("INSERT OR REPLACE INTO model_visibility VALUES (?,?,?)", (peer, model, int(shown)))

    def catalog(self):
        with self.lock:
            return [{"id": r["id"], "title": r["title"],
                     "time": {"created": r["created"], "updated": r["updated"]}}
                    for r in self.db.execute("SELECT * FROM chats WHERE thread IS NOT NULL ORDER BY updated DESC")]

    def message(self, mid, sid, role, text, speaker):
        now = int(time.time() * 1000)
        with self.lock, self.db:
            self.db.execute("""INSERT INTO messages VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET text=excluded.text""", (mid, sid, role, text, speaker, now))
            self.db.execute("UPDATE chats SET updated=? WHERE id=?", (now, sid))

    def history(self, sid):
        with self.lock:
            return [{"info": {"id": r["id"], "role": r["role"], "speaker": r["speaker"], "time": {"created": r["created"]}},
                     "parts": [{"type": "text", "text": r["text"]}]}
                    for r in self.db.execute("SELECT * FROM messages WHERE sid=? ORDER BY rowid", (sid,))]

    def delete(self, sid):
        with self.lock, self.db:
            self.db.execute("DELETE FROM chats WHERE id=?", (sid,))
            self.db.execute("DELETE FROM messages WHERE sid=?", (sid,))
            self.db.execute("DELETE FROM contacts WHERE sid=?", (sid,))
