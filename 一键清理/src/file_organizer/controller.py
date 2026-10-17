from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QUrl
from PySide6.QtWidgets import QFileDialog

from .core import (
    BatchResult,
    FileItem,
    PreviewItem,
    ScanResult,
    build_preview,
    execute_preview,
    scan_directories,
    undo_moves,
    validate_directory_pair,
)
from .store import LocalStore, SettingsStore


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _age_label(created_ts: float) -> str:
    days = max(0, int((time.time() - created_ts) // 86400))
    if days < 30:
        return f"{days} 天"
    if days < 365:
        months = days // 30
        return f"{months} 个月"
    years, remainder = divmod(days, 365)
    months = remainder // 30
    return f"{years} 年" if months == 0 else f"{years} 年 {months} 个月"


class FileListModel(QAbstractListModel):
    NameRole = int(Qt.ItemDataRole.UserRole) + 1
    CategoryRole = NameRole + 1
    SizeRole = NameRole + 2
    CreatedRole = NameRole + 3
    AgeRole = NameRole + 4
    PathRole = NameRole + 5
    PendingRole = NameRole + 6
    LocationRole = NameRole + 7

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._items: list[FileItem] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.NameRole: b"name",
            self.CategoryRole: b"categoryLabel",
            self.SizeRole: b"sizeLabel",
            self.CreatedRole: b"createdLabel",
            self.AgeRole: b"ageLabel",
            self.PathRole: b"path",
            self.PendingRole: b"pendingDelete",
            self.LocationRole: b"locationKind",
        }

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role in (self.NameRole, int(Qt.ItemDataRole.DisplayRole)):
            return item.name
        if role == self.CategoryRole:
            return item.category_label
        if role == self.SizeRole:
            return _size_label(item.size)
        if role == self.CreatedRole:
            return datetime.fromtimestamp(item.created_ts).strftime("%Y-%m-%d")
        if role == self.AgeRole:
            return _age_label(item.created_ts)
        if role == self.PathRole:
            return item.path
        if role == self.PendingRole:
            return item.pending_delete
        if role == self.LocationRole:
            return item.location_kind
        return None

    def replace(self, items: list[FileItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> FileItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def update_mark(self, path: str, marked: bool) -> None:
        for row, item in enumerate(self._items):
            if os.path.normcase(os.path.abspath(item.path)) == os.path.normcase(os.path.abspath(path)):
                self._items[row] = FileItem(**{**item.__dict__, "pending_delete": marked})
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [self.PendingRole])
                return


class PreviewListModel(QAbstractListModel):
    NameRole = int(Qt.ItemDataRole.UserRole) + 1
    CategoryRole = NameRole + 1
    SizeRole = NameRole + 2
    SourceRole = NameRole + 3
    DestinationRole = NameRole + 4
    SelectedRole = NameRole + 5

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._items: list[PreviewItem] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.NameRole: b"name",
            self.CategoryRole: b"categoryLabel",
            self.SizeRole: b"sizeLabel",
            self.SourceRole: b"sourcePath",
            self.DestinationRole: b"destinationPath",
            self.SelectedRole: b"selected",
        }

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role in (self.NameRole, int(Qt.ItemDataRole.DisplayRole)):
            return item.name
        if role == self.CategoryRole:
            from .core import CATEGORY_NAMES
            return CATEGORY_NAMES[item.category]
        if role == self.SizeRole:
            return _size_label(item.size)
        if role == self.SourceRole:
            return item.source_path
        if role == self.DestinationRole:
            return item.destination_path
        if role == self.SelectedRole:
            return item.selected
        return None

    def replace(self, items: tuple[PreviewItem, ...]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def set_selected(self, row: int, selected: bool) -> None:
        if 0 <= row < len(self._items):
            item = self._items[row]
            self._items[row] = PreviewItem(**{**item.__dict__, "selected": selected})
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [self.SelectedRole])

    def selected_items(self) -> tuple[PreviewItem, ...]:
        return tuple(item for item in self._items if item.selected)

    def selected_count(self) -> int:
        return sum(item.selected for item in self._items)


class TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class FunctionTask(QRunnable):
    def __init__(self, function: Callable[[], object]):
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:  # Surface filesystem errors in the UI.
            self.signals.failed.emit(str(exc))
        else:
            self.signals.finished.emit(result)


class AppController(QObject):
    sourcePathChanged = Signal()
    archivePathChanged = Signal()
    sourceCountChanged = Signal()
    archiveCountChanged = Signal()
    pendingCountChanged = Signal()
    busyChanged = Signal()
    statusChanged = Signal()
    skippedCountChanged = Signal()
    lastScanChanged = Signal()
    themeChanged = Signal()
    setupRequiredChanged = Signal()
    undoAvailableChanged = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.store = LocalStore()
        self.settings = SettingsStore()
        self.files_model = FileListModel(self)
        self.preview_model = PreviewListModel(self)
        self.pool = QThreadPool.globalInstance()
        self._source_path = str(self.settings.get("source_path", ""))
        self._archive_path = str(self.settings.get("archive_path", ""))
        self._active_location = "source"
        self._busy = False
        self._status = "选择目录后开始扫描。"
        self._skipped_count = 0
        self._last_scan = float(self.settings.get("last_scan", 0) or 0)
        self._theme = str(self.settings.get("theme", "light"))
        self._setup_required = not (self._source_path and self._archive_path)
        self._source_count, self._archive_count, self._pending_count = self.store.counts()
        self._preview_date_mode = "created"
        self._preview_batch_id = ""
        self.store.recover_interrupted_batch()
        self._refresh_model()

    def _set_status(self, message: str) -> None:
        self._status = message
        self.statusChanged.emit()

    def _set_busy(self, busy: bool) -> None:
        if self._busy != busy:
            self._busy = busy
            self.busyChanged.emit()

    def _refresh_counts(self) -> None:
        self._source_count, self._archive_count, self._pending_count = self.store.counts()
        self.sourceCountChanged.emit()
        self.archiveCountChanged.emit()
        self.pendingCountChanged.emit()
        self.undoAvailableChanged.emit()

    def _refresh_model(self) -> None:
        self.files_model.replace(self.store.list_files(self._active_location))

    def _start_task(self, function: Callable[[], object], on_success: Callable[[object], None]) -> None:
        self._set_busy(True)
        task = FunctionTask(function)
        task.signals.finished.connect(lambda result: self._task_finished(result, on_success))
        task.signals.failed.connect(self._task_failed)
        self.pool.start(task)

    def _task_finished(self, result: object, on_success: Callable[[object], None]) -> None:
        self._set_busy(False)
        on_success(result)

    def _task_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_status(f"操作失败：{message}")

    def _counts_status(self) -> str:
        return f"来源 {self._source_count} 项 · 归档 {self._archive_count} 项 · 待复核 {self._pending_count} 项"

    @Property(str, notify=sourcePathChanged)
    def sourcePath(self) -> str:
        return self._source_path

    @Property(str, notify=archivePathChanged)
    def archivePath(self) -> str:
        return self._archive_path

    @Property(int, notify=sourceCountChanged)
    def sourceCount(self) -> int:
        return self._source_count

    @Property(int, notify=archiveCountChanged)
    def archiveCount(self) -> int:
        return self._archive_count

    @Property(int, notify=pendingCountChanged)
    def pendingCount(self) -> int:
        return self._pending_count

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(int, notify=skippedCountChanged)
    def skippedCount(self) -> int:
        return self._skipped_count

    @Property(str, notify=lastScanChanged)
    def lastScan(self) -> str:
        return datetime.fromtimestamp(self._last_scan).strftime("%Y-%m-%d %H:%M") if self._last_scan else "尚未扫描"

    @Property(bool, notify=themeChanged)
    def darkTheme(self) -> bool:
        return self._theme == "dark"

    @Property(bool, notify=setupRequiredChanged)
    def setupRequired(self) -> bool:
        return self._setup_required

    @Property(QObject, constant=True)
    def filesModel(self) -> QObject:
        return self.files_model

    @Property(QObject, constant=True)
    def previewModel(self) -> QObject:
        return self.preview_model

    @Property(bool, notify=undoAvailableChanged)
    def undoAvailable(self) -> bool:
        return self.store.latest_undoable_batch() is not None

    @Slot(str, result=str)
    def chooseFolder(self, initial: str = "") -> str:
        selected = QFileDialog.getExistingDirectory(None, "选择文件夹", initial or str(Path.home()))
        return selected

    @Slot(str, str, result=bool)
    def configurePaths(self, source: str, archive: str) -> bool:
        try:
            source_path, archive_path = validate_directory_pair(source, archive)
        except (OSError, ValueError) as exc:
            self._set_status(str(exc))
            return False
        self._source_path, self._archive_path = str(source_path), str(archive_path)
        self.settings.set_many(source_path=self._source_path, archive_path=self._archive_path)
        self._setup_required = False
        self.sourcePathChanged.emit()
        self.archivePathChanged.emit()
        self.setupRequiredChanged.emit()
        self._set_status("目录已保存。先扫描并检查清单，再预览归档。")
        return True

    @Slot(str)
    def setActiveLocation(self, location: str) -> None:
        if location not in {"source", "archive"}:
            return
        self._active_location = location
        self._refresh_model()

    @Slot(result=bool)
    def scan(self) -> bool:
        if self._busy:
            return False
        if not self._source_path or not self._archive_path:
            self._setup_required = True
            self.setupRequiredChanged.emit()
            self._set_status("请先选择来源和归档目录。")
            return False
        self._set_status("正在扫描所选目录……")

        def work() -> ScanResult:
            return scan_directories(self._source_path, self._archive_path)

        def complete(result: object) -> None:
            assert isinstance(result, ScanResult)
            self.store.upsert_scan(result.source_files, result.archive_files, (self._source_path, self._archive_path))
            self._skipped_count = result.skipped_count
            self.skippedCountChanged.emit()
            self._last_scan = result.scanned_at
            self.settings.set_many(last_scan=self._last_scan)
            self.lastScanChanged.emit()
            self._refresh_counts()
            self._refresh_model()
            issue_count = len(result.issues)
            suffix = f"另有 {issue_count} 个目录/文件读取问题。" if issue_count else ""
            self._set_status(f"扫描完成：来源 {len(result.source_files)} 项，归档 {len(result.archive_files)} 项，跳过 {result.skipped_count} 项。{suffix}")

        self._start_task(work, complete)
        return True

    @Slot(bool)
    def setPendingDelete(self, row: int, marked: bool) -> None:
        item = self.files_model.item_at(row)
        if item is None:
            return
        self.store.set_pending_delete(item.path, marked)
        self.files_model.update_mark(item.path, marked)
        self._refresh_counts()

    @Slot(str, result=int)
    def createPreview(self, date_mode: str) -> int:
        if self._busy:
            return 0
        if not self._source_path or not self._archive_path:
            self._set_status("请先选择来源和归档目录。")
            return 0
        try:
            items = self.store.list_files("source")
            preview = build_preview(items, self._archive_path, date_mode)
        except (OSError, ValueError) as exc:
            self._set_status(f"无法生成预览：{exc}")
            return 0
        self.preview_model.replace(preview)
        self._preview_date_mode = date_mode
        self._preview_batch_id = uuid.uuid4().hex
        return len(preview)

    @Slot(int, bool)
    def setPreviewSelected(self, row: int, selected: bool) -> None:
        self.preview_model.set_selected(row, selected)

    @Slot(result=int)
    def selectedPreviewCount(self) -> int:
        return self.preview_model.selected_count()

    @Slot(result=bool)
    def confirmArchive(self) -> bool:
        if self._busy:
            return False
        selected = self.preview_model.selected_items()
        if not selected:
            self._set_status("请至少勾选一个文件。")
            return False
        self.store.begin_batch(self._preview_batch_id, self._preview_date_mode, selected)
        batch_id = self._preview_batch_id

        def work() -> BatchResult:
            return execute_preview(selected, self._preview_date_mode, batch_id)

        def complete(result: object) -> None:
            assert isinstance(result, BatchResult)
            self.store.finish_batch(result)
            self._refresh_counts()
            self._refresh_model()
            self._set_status(
                f"归档完成：移动 {result.moved_count} 项，跳过 {result.skipped_count} 项，失败 {result.failed_count} 项。"
            )

        self._set_status("正在按预览执行归档……")
        self._start_task(work, complete)
        return True

    @Slot(result=bool)
    def undoLastBatch(self) -> bool:
        if self._busy:
            return False
        last = self.store.latest_undoable_batch()
        if last is None:
            self._set_status("没有可撤销的归档批次。")
            return False
        batch_id, moves = last
        self._set_status("正在撤销最近一批归档……")

        def work() -> tuple[str, tuple[object, ...]]:
            return batch_id, undo_moves(moves)

        def complete(result: object) -> None:
            completed_batch_id, outcomes = result
            packed = tuple((item.source_path, item.destination_path, item.status, item.message) for item in outcomes)
            self.store.finish_undo(completed_batch_id, packed)
            moved = sum(item.status == "moved" for item in outcomes)
            skipped = sum(item.status == "skipped" for item in outcomes)
            failed = sum(item.status == "failed" for item in outcomes)
            self._refresh_counts()
            self._refresh_model()
            self._set_status(f"撤销完成：还原 {moved} 项，跳过 {skipped} 项，失败 {failed} 项。")

        self._start_task(work, complete)
        return True

    @Slot(str)
    def openFile(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @Slot(str)
    def revealFile(self, path: str) -> None:
        if os.name == "nt":
            try:
                subprocess.Popen(["explorer.exe", f"/select,{path}"])
            except OSError as exc:
                self._set_status(f"无法打开资源管理器：{exc}")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    @Slot(bool)
    def setTheme(self, dark: bool) -> None:
        self._theme = "dark" if dark else "light"
        self.settings.set_many(theme=self._theme)
        self.themeChanged.emit()

    @Slot(result=str)
    def latestBackupLabel(self) -> str:
        backup = self.store.latest_backup()
        return backup.stat().st_mtime and datetime.fromtimestamp(backup.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if backup else "暂无本地备份"

    @Slot(result=bool)
    def restoreLatestBackup(self) -> bool:
        if self._busy:
            return False
        try:
            restored = self.store.restore_latest_backup()
        except (OSError, sqlite3.Error) as exc:  # type: ignore[name-defined]
            self._set_status(f"恢复备份失败：{exc}")
            return False
        if not restored:
            self._set_status("尚无可恢复的数据库备份。")
            return False
        self._refresh_counts()
        self._refresh_model()
        self._set_status("已从最近一份本地数据库备份恢复索引和操作历史。")
        return True

    @Slot()
    def clearUnavailableRecords(self) -> None:
        count = self.store.clear_unavailable()
        self._refresh_counts()
        self._refresh_model()
        self._set_status(f"已清除 {count} 条不可用位置记录。")

