from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


CATEGORY_NAMES = {
    "images": "图片",
    "videos": "视频",
    "word": "Word",
    "excel": "Excel",
    "powerpoint": "PowerPoint",
    "pdf": "PDF",
    "other": "其他",
}

EXTENSION_CATEGORIES = {
    **dict.fromkeys({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif", ".svg", ".raw", ".cr2", ".nef", ".arw", ".dng"}, "images"),
    **dict.fromkeys({".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".mpg", ".mpeg", ".mts", ".3gp"}, "videos"),
    **dict.fromkeys({".doc", ".docx", ".docm", ".dot", ".dotx", ".rtf", ".odt"}, "word"),
    **dict.fromkeys({".xls", ".xlsx", ".xlsm", ".xlsb", ".csv", ".ods"}, "excel"),
    **dict.fromkeys({".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".odp"}, "powerpoint"),
    ".pdf": "pdf",
}

ONE_GIB = 1024**3
VERIFY_TIME_TOLERANCE_SECONDS = 2.1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SafetyError(ValueError):
    """Raised when a selected path violates the app's safety boundaries."""


@dataclass(frozen=True)
class ScanIssue:
    path: str
    message: str


@dataclass(frozen=True)
class FileItem:
    path: str
    name: str
    category: str
    size: int
    created_ts: float
    modified_ts: float
    modified_ns: int
    location_kind: str
    pending_delete: bool = False
    available: bool = True

    @property
    def category_label(self) -> str:
        return CATEGORY_NAMES.get(self.category, CATEGORY_NAMES["other"])


@dataclass(frozen=True)
class ScanResult:
    source_files: tuple[FileItem, ...]
    archive_files: tuple[FileItem, ...]
    skipped_count: int
    issues: tuple[ScanIssue, ...]
    scanned_at: float


@dataclass(frozen=True)
class PreviewItem:
    source_path: str
    destination_path: str
    name: str
    category: str
    size: int
    created_ts: float
    modified_ts: float
    modified_ns: int
    location_kind: str = "source"
    selected: bool = True


@dataclass(frozen=True)
class MoveOutcome:
    source_path: str
    destination_path: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    date_mode: str
    outcomes: tuple[MoveOutcome, ...]
    created_at: float = field(default_factory=time.time)

    @property
    def moved_count(self) -> int:
        return sum(item.status == "moved" for item in self.outcomes)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.outcomes)

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.outcomes)


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_relative_to(path: str | Path, root: str | Path) -> bool:
    try:
        return os.path.commonpath([_canonical(path), _canonical(root)]) == _canonical(root)
    except (ValueError, OSError):
        return False


def _is_network_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\"):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path.drive + "\\")) == 4
    except (AttributeError, OSError):
        return False


def _known_onedrive_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value).expanduser())
    default = Path.home() / "OneDrive"
    if default.exists():
        roots.append(default)
    return tuple(roots)


def _is_reparse_or_link(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    attrs = getattr(details, "st_file_attributes", 0)
    return path.is_symlink() or bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _has_hidden_or_system_attribute(path: Path, details: os.stat_result | None = None) -> bool:
    if path.name.startswith("."):
        return True
    if details is None:
        try:
            details = path.stat(follow_symlinks=False)
        except (OSError, TypeError):
            return False
    attrs = getattr(details, "st_file_attributes", 0)
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def validate_directory_pair(source: str | Path, archive: str | Path) -> tuple[Path, Path]:
    source_input = Path(source).expanduser()
    archive_input = Path(archive).expanduser()
    if _is_reparse_or_link(source_input) or _is_reparse_or_link(archive_input):
        raise SafetyError("来源或归档目录不能是符号链接、目录联接点或其他重解析点。")
    source_path = source_input.resolve(strict=True)
    archive_path = archive_input.resolve(strict=True)
    if not source_path.is_dir() or not archive_path.is_dir():
        raise SafetyError("来源和归档位置都必须是已存在的文件夹。")
    if source_path == Path(source_path.anchor):
        raise SafetyError("不能把磁盘根目录作为来源；请选择其中一个具体文件夹。")
    if _is_network_path(source_path) or _is_network_path(archive_path):
        raise SafetyError("不支持网络共享或映射的网络磁盘。")
    for candidate in (source_path, archive_path):
        if _is_reparse_or_link(candidate):
            raise SafetyError("来源或归档目录不能是符号链接、目录联接点或其他重解析点。")
        for root in _known_onedrive_roots():
            if _is_relative_to(candidate, root):
                raise SafetyError("检测到 OneDrive 路径。为避免同步冲突，暂不支持在 OneDrive 中整理。")
    if _canonical(source_path) == _canonical(archive_path):
        raise SafetyError("来源目录和归档目录不能相同。")
    if _is_relative_to(source_path, archive_path):
        raise SafetyError("归档目录不能是来源目录的上级目录。")
    return source_path, archive_path


def category_for(path: str | Path) -> str:
    return EXTENSION_CATEGORIES.get(Path(path).suffix.casefold(), "other")


def _created_timestamp(details: os.stat_result) -> float:
    birth = getattr(details, "st_birthtime", None)
    if birth is not None:
        return float(birth)
    # On Windows st_ctime is creation time. On other platforms it is the closest
    # available fallback; the app is Windows-first and keeps this value in its DB.
    return float(details.st_ctime)


def _to_item(path: Path, details: os.stat_result, location_kind: str) -> FileItem:
    return FileItem(
        path=str(path),
        name=path.name,
        category=category_for(path),
        size=int(details.st_size),
        created_ts=_created_timestamp(details),
        modified_ts=float(details.st_mtime),
        modified_ns=int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1_000_000_000))),
        location_kind=location_kind,
    )


def scan_directories(source: str | Path, archive: str | Path) -> ScanResult:
    source_path, archive_path = validate_directory_pair(source, archive)
    skipped = 0
    issues: list[ScanIssue] = []

    def walk(root: Path, location_kind: str, excluded: Path | None = None) -> list[FileItem]:
        nonlocal skipped
        found: list[FileItem] = []
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError as exc:
                issues.append(ScanIssue(str(folder), f"无法读取目录：{exc.strerror or exc}"))
                continue
            for entry in entries:
                candidate = Path(entry.path)
                if excluded is not None and _is_relative_to(candidate, excluded):
                    skipped += 1
                    continue
                try:
                    if entry.is_symlink():
                        skipped += 1
                        continue
                    details = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    issues.append(ScanIssue(str(candidate), f"无法读取项目：{exc.strerror or exc}"))
                    continue
                attrs = getattr(details, "st_file_attributes", 0)
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                    skipped += 1
                    continue
                if _has_hidden_or_system_attribute(candidate, details):
                    skipped += 1
                    continue
                if stat.S_ISDIR(details.st_mode):
                    stack.append(candidate)
                elif stat.S_ISREG(details.st_mode):
                    found.append(_to_item(candidate, details, location_kind))
                else:
                    skipped += 1
        found.sort(key=lambda item: (item.created_ts, item.name.casefold(), item.path.casefold()))
        return found

    source_files = walk(source_path, "source", archive_path if _is_relative_to(archive_path, source_path) else None)
    archive_files = walk(archive_path, "archive")
    return ScanResult(tuple(source_files), tuple(archive_files), skipped, tuple(issues), time.time())


def _unique_destination(candidate: Path, reserved: set[str]) -> Path:
    test = candidate
    index = 2
    while _canonical(test) in reserved or test.exists():
        suffix = test.suffix
        stem = test.stem
        test = test.with_name(f"{stem} ({index}){suffix}")
        index += 1
    reserved.add(_canonical(test))
    return test


def build_preview(items: Iterable[FileItem], archive: str | Path, date_mode: str = "created") -> tuple[PreviewItem, ...]:
    if date_mode not in {"created", "modified"}:
        raise ValueError("日期规则必须是 created 或 modified。")
    archive_path = Path(archive).expanduser().resolve(strict=True)
    reserved: set[str] = set()
    preview: list[PreviewItem] = []
    for item in items:
        if item.location_kind != "source" or not item.available:
            continue
        timestamp = item.created_ts if date_mode == "created" else item.modified_ts
        date = datetime.fromtimestamp(timestamp)
        folder = archive_path / f"{date.year:04d}" / f"{date.month:02d}" / CATEGORY_NAMES[item.category]
        destination = _unique_destination(folder / item.name, reserved)
        preview.append(
            PreviewItem(
                source_path=item.path,
                destination_path=str(destination),
                name=item.name,
                category=item.category,
                size=item.size,
                created_ts=item.created_ts,
                modified_ts=item.modified_ts,
                modified_ns=item.modified_ns,
            )
        )
    return tuple(preview)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_signature(path: Path, item: PreviewItem) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and int(details.st_size) == item.size
        and int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1_000_000_000))) == item.modified_ns
    )


def _move_without_overwrite(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"目标已存在：{destination}")
    if os.name == "nt":
        # Windows os.rename fails when the destination exists, unlike os.replace.
        os.rename(source, destination)
        return
    # A hard link is atomic and fails if the target exists. The unlink completes
    # a same-volume move without a replace race on non-Windows test hosts.
    os.link(source, destination, follow_symlinks=False)
    try:
        source.unlink()
    except OSError:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _copy_verify_and_move(source: Path, destination: Path, item: PreviewItem) -> None:
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    source_before = source.stat(follow_symlinks=False)
    if int(source_before.st_size) != item.size or int(source_before.st_mtime_ns) != item.modified_ns:
        raise RuntimeError("来源文件在预览后发生变化。")
    try:
        with source.open("rb") as input_stream, temp.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        shutil.copystat(source, temp, follow_symlinks=False)
        source_after_copy = source.stat(follow_symlinks=False)
        temp_details = temp.stat(follow_symlinks=False)
        if int(source_after_copy.st_size) != item.size or int(source_after_copy.st_mtime_ns) != item.modified_ns:
            raise RuntimeError("来源文件复制期间发生变化，已保留来源文件。")
        if int(temp_details.st_size) != item.size:
            raise RuntimeError("复制后文件大小不匹配，已保留来源文件。")
        if item.size <= ONE_GIB:
            if _hash_file(source) != _hash_file(temp):
                raise RuntimeError("SHA-256 校验失败，已保留来源文件。")
        elif abs(temp_details.st_mtime - source_after_copy.st_mtime) > VERIFY_TIME_TOLERANCE_SECONDS:
            raise RuntimeError("大文件的修改时间校验失败，已保留来源文件。")
        if destination.exists():
            raise FileExistsError(f"目标在复制期间已被创建：{destination}")
        _move_without_overwrite(temp, destination)
        try:
            source.unlink()
        except OSError:
            # If source removal fails, remove only our newly published copy so
            # the visible result remains the original source file.
            try:
                destination.unlink()
            except OSError:
                pass
            raise
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def execute_preview(items: Iterable[PreviewItem], date_mode: str, batch_id: str | None = None) -> BatchResult:
    outcomes: list[MoveOutcome] = []
    for item in items:
        if not item.selected:
            continue
        source = Path(item.source_path)
        destination = Path(item.destination_path)
        if not _same_signature(source, item):
            outcomes.append(MoveOutcome(item.source_path, item.destination_path, "skipped", "文件已消失或在预览后发生变化。"))
            continue
        if destination.exists():
            outcomes.append(MoveOutcome(item.source_path, item.destination_path, "skipped", "目标位置已被占用；为避免覆盖，已跳过。"))
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_device = source.stat(follow_symlinks=False).st_dev
            destination_device = destination.parent.stat().st_dev
            if source_device == destination_device:
                _move_without_overwrite(source, destination)
            else:
                _copy_verify_and_move(source, destination, item)
        except (OSError, RuntimeError) as exc:
            outcomes.append(MoveOutcome(item.source_path, item.destination_path, "failed", str(exc)))
        else:
            outcomes.append(MoveOutcome(item.source_path, item.destination_path, "moved"))
    return BatchResult(batch_id or uuid.uuid4().hex, date_mode, tuple(outcomes))


def undo_moves(moves: Iterable[tuple[str, str, int, float, int]]) -> tuple[MoveOutcome, ...]:
    outcomes: list[MoveOutcome] = []
    for destination_text, source_text, size, modified_ts, modified_ns in moves:
        destination = Path(destination_text)
        source = Path(source_text)
        if not destination.exists():
            outcomes.append(MoveOutcome(destination_text, source_text, "skipped", "归档文件已不存在。"))
            continue
        if source.exists():
            outcomes.append(MoveOutcome(destination_text, source_text, "skipped", "原路径已被占用。"))
            continue
        try:
            details = destination.stat(follow_symlinks=False)
            if int(details.st_size) != size or int(details.st_mtime_ns) != modified_ns:
                outcomes.append(MoveOutcome(destination_text, source_text, "skipped", "归档文件在移动后发生变化。"))
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            if details.st_dev == source.parent.stat().st_dev:
                _move_without_overwrite(destination, source)
            else:
                item = PreviewItem(
                    source_path=destination_text,
                    destination_path=source_text,
                    name=destination.name,
                    category=category_for(destination),
                    size=size,
                    created_ts=modified_ts,
                    modified_ts=modified_ts,
                    modified_ns=modified_ns,
                )
                _copy_verify_and_move(destination, source, item)
        except (OSError, RuntimeError) as exc:
            outcomes.append(MoveOutcome(destination_text, source_text, "failed", str(exc)))
        else:
            outcomes.append(MoveOutcome(destination_text, source_text, "moved"))
    return tuple(outcomes)
