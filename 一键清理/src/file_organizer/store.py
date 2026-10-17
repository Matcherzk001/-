from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .core import BatchResult, FileItem, PreviewItem, _canonical

SCHEMA_VERSION = 1


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "YiJianQingLi"
    return Path.home() / "AppData" / "Local" / "YiJianQingLi"


class LocalStore:
    """SQLite index, operation history, and rotating local backups."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path) if database_path else app_data_dir() / "index.sqlite3"
        self.data_dir = self.database_path.parent
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.data_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        existed = self.database_path.exists() and self.database_path.stat().st_size > 0
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if existed and version < SCHEMA_VERSION:
            self._backup_connection("before-migration")
        self._create_schema()
        self.recover_interrupted_batch()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path_key TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_ts REAL NOT NULL,
                    modified_ts REAL NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    location_kind TEXT NOT NULL CHECK(location_kind IN ('source','archive')),
                    pending_delete INTEGER NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 1,
                    last_seen_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_files_available_location
                    ON files(available, location_kind, created_ts, name);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    date_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    moved_count INTEGER NOT NULL DEFAULT 0,
                    undone_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    destination_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_ts REAL NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    undone INTEGER NOT NULL DEFAULT 0
                );
                PRAGMA user_version = 1;
                """
            )

    def _backup_connection(self, label: str = "batch") -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.backup_dir / f"index-{stamp}-{label}.sqlite3"
        destination = sqlite3.connect(target)
        try:
            self.connection.backup(destination)
        finally:
            destination.close()
        self._prune_backups()
        return target

    def _prune_backups(self) -> None:
        backups = sorted(self.backup_dir.glob("index-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[3:]:
            try:
                old.unlink()
            except OSError:
                pass

    def backup_now(self, label: str = "manual") -> Path | None:
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return None
        return self._backup_connection(label)

    def latest_backup(self) -> Path | None:
        backups = sorted(self.backup_dir.glob("index-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        return backups[0] if backups else None

    def restore_latest_backup(self) -> bool:
        backup = self.latest_backup()
        if backup is None:
            return False
        restored = self.database_path.with_suffix(".restore.sqlite3")
        shutil.copy2(backup, restored)
        self.backup_now("before-restore")
        self.close()
        os.replace(restored, self.database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        return True

    def upsert_scan(self, source_files: Iterable[FileItem], archive_files: Iterable[FileItem], roots: tuple[str, str]) -> None:
        items = tuple(source_files) + tuple(archive_files)
        seen = {_canonical(item.path) for item in items}
        now = time.time()
        with self.connection:
            for item in items:
                key = _canonical(item.path)
                self.connection.execute(
                    """
                    INSERT INTO files(path_key,path,original_path,name,category,size,created_ts,modified_ts,modified_ns,
                                      location_kind,pending_delete,available,last_seen_ts)
                    VALUES(?,?,?,?,?,?,?,?,?,?,0,1,?)
                    ON CONFLICT(path_key) DO UPDATE SET
                        path=excluded.path,
                        name=excluded.name,
                        category=excluded.category,
                        size=excluded.size,
                        created_ts=CASE WHEN excluded.location_kind='archive' THEN files.created_ts ELSE excluded.created_ts END,
                        modified_ts=excluded.modified_ts,
                        modified_ns=excluded.modified_ns,
                        location_kind=excluded.location_kind,
                        available=1,
                        last_seen_ts=excluded.last_seen_ts
                    """,
                    (key, item.path, item.path, item.name, item.category, item.size, item.created_ts,
                     item.modified_ts, item.modified_ns, item.location_kind, now),
                )
            for root in roots:
                root_key = _canonical(root).rstrip("\\/")
                rows = self.connection.execute("SELECT path_key FROM files WHERE available=1").fetchall()
                for row in rows:
                    key = row["path_key"]
                    if key == root_key or key.startswith(root_key + os.sep):
                        if key not in seen:
                            self.connection.execute("UPDATE files SET available=0 WHERE path_key=?", (key,))

    def list_files(self, location_kind: str) -> list[FileItem]:
        rows = self.connection.execute(
            "SELECT * FROM files WHERE available=1 AND location_kind=? ORDER BY created_ts ASC, name COLLATE NOCASE ASC",
            (location_kind,),
        ).fetchall()
        return [
            FileItem(
                path=row["path"], name=row["name"], category=row["category"], size=row["size"],
                created_ts=row["created_ts"], modified_ts=row["modified_ts"], modified_ns=row["modified_ns"],
                location_kind=row["location_kind"], pending_delete=bool(row["pending_delete"]), available=bool(row["available"]),
            )
            for row in rows
        ]

    def counts(self) -> tuple[int, int, int]:
        rows = self.connection.execute(
            "SELECT location_kind, COUNT(*) AS total, SUM(pending_delete) AS marked FROM files WHERE available=1 GROUP BY location_kind"
        ).fetchall()
        source_count = archive_count = pending_count = 0
        for row in rows:
            if row["location_kind"] == "source":
                source_count = int(row["total"])
            elif row["location_kind"] == "archive":
                archive_count = int(row["total"])
            pending_count += int(row["marked"] or 0)
        return source_count, archive_count, pending_count

    def set_pending_delete(self, path: str, marked: bool) -> None:
        with self.connection:
            self.connection.execute("UPDATE files SET pending_delete=? WHERE path_key=?", (int(marked), _canonical(path)))

    def begin_batch(self, batch_id: str, date_mode: str, selected: Iterable[PreviewItem]) -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO batches(id,created_at,date_mode,status) VALUES(?,?,?,'running')",
                (batch_id, now, date_mode),
            )
            for item in selected:
                self.connection.execute(
                    """INSERT INTO moves(batch_id,source_path,destination_path,size,modified_ts,modified_ns,status)
                       VALUES(?,?,?,?,?,?,'pending')""",
                    (batch_id, item.source_path, item.destination_path, item.size, item.modified_ts, item.modified_ns),
                )

    def finish_batch(self, result: BatchResult) -> None:
        with self.connection:
            for outcome in result.outcomes:
                row = self.connection.execute(
                    "SELECT * FROM moves WHERE batch_id=? AND source_path=? AND destination_path=?",
                    (result.batch_id, outcome.source_path, outcome.destination_path),
                ).fetchone()
                if row is None:
                    continue
                self.connection.execute(
                    "UPDATE moves SET status=?,message=? WHERE id=?",
                    (outcome.status, outcome.message, row["id"]),
                )
                if outcome.status == "moved":
                    old_key = _canonical(outcome.source_path)
                    new_key = _canonical(outcome.destination_path)
                    collision = self.connection.execute("SELECT path_key,available FROM files WHERE path_key=?", (new_key,)).fetchone()
                    if collision and not collision["available"]:
                        self.connection.execute("DELETE FROM files WHERE path_key=?", (new_key,))
                    self.connection.execute(
                        """UPDATE files SET path_key=?,path=?,location_kind='archive',available=1,last_seen_ts=?
                           WHERE path_key=?""",
                        (new_key, outcome.destination_path, time.time(), old_key),
                    )
            moved = int(self.connection.execute("SELECT COUNT(*) FROM moves WHERE batch_id=? AND status='moved'", (result.batch_id,)).fetchone()[0])
            self.connection.execute("UPDATE batches SET status='completed',moved_count=? WHERE id=?", (moved, result.batch_id))
        if moved:
            self.backup_now("batch")

    def recover_interrupted_batch(self) -> None:
        rows = self.connection.execute(
            "SELECT m.*,b.id AS batch FROM moves m JOIN batches b ON b.id=m.batch_id WHERE m.status='pending' AND b.status='running'"
        ).fetchall()
        if not rows:
            return
        with self.connection:
            for row in rows:
                source = Path(row["source_path"])
                destination = Path(row["destination_path"])
                if not source.exists() and destination.exists():
                    old_key = _canonical(source)
                    new_key = _canonical(destination)
                    collision = self.connection.execute("SELECT available FROM files WHERE path_key=?", (new_key,)).fetchone()
                    if collision and not collision["available"]:
                        self.connection.execute("DELETE FROM files WHERE path_key=?", (new_key,))
                    self.connection.execute(
                        "UPDATE files SET path_key=?,path=?,location_kind='archive',available=1,last_seen_ts=? WHERE path_key=?",
                        (new_key, str(destination), time.time(), old_key),
                    )
                    self.connection.execute("UPDATE moves SET status='moved',message='Recovered after restart' WHERE id=?", (row["id"],))
                else:
                    self.connection.execute("UPDATE moves SET status='failed',message='Interrupted before move completed' WHERE id=?", (row["id"],))
            self.connection.execute(
                "UPDATE batches SET status='completed',moved_count=(SELECT COUNT(*) FROM moves WHERE batch_id=batches.id AND status='moved') WHERE status='running'"
            )

    def latest_undoable_batch(self) -> tuple[str, list[tuple[str, str, int, float, int]]] | None:
        batch = self.connection.execute(
            "SELECT id FROM batches WHERE status='completed' AND moved_count>undone_count ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if batch is None:
            return None
        rows = self.connection.execute(
            "SELECT destination_path,source_path,size,modified_ts,modified_ns FROM moves WHERE batch_id=? AND status='moved' AND undone=0 ORDER BY id DESC",
            (batch["id"],),
        ).fetchall()
        return batch["id"], [(row["destination_path"], row["source_path"], row["size"], row["modified_ts"], row["modified_ns"]) for row in rows]

    def finish_undo(self, batch_id: str, outcomes: Iterable[tuple[str, str, str, str]]) -> None:
        moved_count = 0
        with self.connection:
            for destination, source, status, message in outcomes:
                row = self.connection.execute(
                    "SELECT * FROM moves WHERE batch_id=? AND destination_path=? AND source_path=? AND status='moved' AND undone=0",
                    (batch_id, destination, source),
                ).fetchone()
                if row is None:
                    continue
                if status == "moved":
                    old_key, new_key = _canonical(destination), _canonical(source)
                    collision = self.connection.execute("SELECT available FROM files WHERE path_key=?", (new_key,)).fetchone()
                    if collision and not collision["available"]:
                        self.connection.execute("DELETE FROM files WHERE path_key=?", (new_key,))
                    self.connection.execute(
                        "UPDATE files SET path_key=?,path=?,location_kind='source',available=1,last_seen_ts=? WHERE path_key=?",
                        (new_key, source, time.time(), old_key),
                    )
                    self.connection.execute("UPDATE moves SET undone=1,message=? WHERE id=?", (message, row["id"]))
                    moved_count += 1
                elif message:
                    self.connection.execute("UPDATE moves SET message=? WHERE id=?", (message, row["id"]))
            self.connection.execute("UPDATE batches SET undone_count=undone_count+? WHERE id=?", (moved_count, batch_id))
        if moved_count:
            self.backup_now("undo")

    def clear_unavailable(self) -> int:
        with self.connection:
            cursor = self.connection.execute("DELETE FROM files WHERE available=0")
        return cursor.rowcount


class SettingsStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else app_data_dir() / "settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._values: dict[str, object] = {}
        if self.path.exists():
            try:
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    self._values = parsed
            except (OSError, json.JSONDecodeError):
                self._values = {}

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)

    def set_many(self, **values: object) -> None:
        self._values.update(values)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._values, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
