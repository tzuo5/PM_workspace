# -*- coding: utf-8 -*-
"""SQLite persistence for PM Workplace project tracking.

The front end still reads/writes the existing ``projects`` shape.  The backend
now also maintains project-centric metadata:

- ``project_aliases``: every stable identifier seen for the same project
  (CQ number, M/K project number, SO number, BT number, etc.).
- ``project_events``: a durable timeline of Outlook evidence used to classify
  the project.
- ``manual_overrides``: audit table reserved for explicit human edits.

This lets early CQ-only workflow mails and later M4367/SO/check/OA mails land on
one project object without changing the UI contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("PM_TRACKER_DB", os.path.join(DATA_DIR, "pm_tracker.db"))

PROJECT_COLUMNS = [
    "id", "contract", "name", "client", "amount", "type", "stage", "date", "notes",
    "favorite", "suspended", "archived", "archivedAt", "archivedFromStage", "stageDates",
    "currentProgress", "latestEmailTime", "latestEmailSubject", "latestSender", "needsReview",
    "reviewReason", "manualOverride", "source", "latestAttachmentDir", "latestEmailEntryId",
    "latestEmailStoreId", "latestEmailFolder", "llmReviewed", "llmSummary", "createdAt", "updatedAt",
]

BOOL_FIELDS = {"favorite", "suspended", "archived", "needsReview", "manualOverride", "llmReviewed"}
JSON_FIELDS = {"stageDates"}


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg",
}

# Display-layer image filter. Outlook often stores signature logos, social icons,
# spacers and pasted formatting assets as normal attachments with names such as
# image001.png. Non-image files are never filtered by these rules; they are only
# de-duplicated.
GENERIC_INLINE_IMAGE_RE = re.compile(
    r"^(?:image|img|pic|picture|oledata|attach|attachment|att|cid|inline|clip_image|微信图片|截图)[-_ ]*\d*$",
    re.I,
)
DECORATIVE_IMAGE_TOKENS = {
    "logo", "logos", "signature", "sign", "sig", "icon", "icons", "banner", "spacer",
    "pixel", "blank", "footer", "header", "linkedin", "facebook", "twitter", "wechat",
    "weixin", "instagram", "youtube", "qrcode", "qr", "disclaimer", "abb",
}
GENERIC_IMAGE_SMALL_BYTES = 96 * 1024
DECORATIVE_IMAGE_MAX_BYTES = 300 * 1024
TINY_IMAGE_BYTES = 12 * 1024
MAX_ATTACHMENT_HASH_BYTES = 50 * 1024 * 1024

ALIAS_TYPE_RULES: Sequence[Tuple[str, str]] = (
    ("PROJECT_NUMBER", r"^[MK]4367-\d{4}$"),
    ("CQ_NUMBER", r"^CQ\d{5,}$"),
    ("SALES_ORDER", r"^50\d{5,}$"),
    ("BT_NUMBER", r"^(?:BT[A-Z]?|RTY)\d{4,}$"),
    ("OCR_NUMBER", r"^OCR-\d{4}$"),
    ("FLOW_SUBJECT", r"^FLOW-[A-F0-9]{8}$"),
)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column when upgrading an existing local SQLite database."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def normalize_alias_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def infer_alias_type(value: Any, fallback: str = "UNKNOWN") -> str:
    normalized = normalize_alias_value(value)
    for alias_type, pattern in ALIAS_TYPE_RULES:
        if re.match(pattern, normalized, re.I):
            return alias_type
    return fallback or "UNKNOWN"


def normalize_alias(alias: Any) -> Optional[Tuple[str, str]]:
    """Accept tuple/list/dict/string alias input and return (type, value)."""
    alias_type = ""
    alias_value = ""
    if isinstance(alias, dict):
        alias_type = str(alias.get("alias_type") or alias.get("type") or "").strip().upper()
        alias_value = str(alias.get("alias_value") or alias.get("value") or "").strip()
    elif isinstance(alias, (list, tuple)) and len(alias) >= 2:
        first, second = alias[0], alias[1]
        # Outlook code historically emits (value, contract_type).  Alias tables
        # need (alias_type, alias_value), so infer when the first item looks like
        # an actual identifier.
        first_value = normalize_alias_value(first)
        second_value = normalize_alias_value(second)
        if infer_alias_type(first_value, ""):
            alias_value = first_value
            alias_type = infer_alias_type(first_value)
        elif infer_alias_type(second_value, ""):
            alias_value = second_value
            alias_type = infer_alias_type(second_value)
        else:
            alias_type = str(first or "").strip().upper()
            alias_value = str(second or "").strip()
    else:
        alias_value = str(alias or "").strip()
    alias_value = normalize_alias_value(alias_value)
    if not alias_value:
        return None
    if not alias_type or alias_type in {"NORMAL", "OCR", "CQ", "SUBJECT", "SALES_ORDER", "BT"}:
        alias_type = infer_alias_type(alias_value, alias_type or "UNKNOWN")
    return alias_type, alias_value



def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _attachment_ext(filename: Any) -> str:
    return os.path.splitext(str(filename or "").strip())[1].lower()


def is_image_attachment(filename: Any) -> bool:
    return _attachment_ext(filename) in IMAGE_EXTENSIONS


def _normalized_attachment_name(filename: Any) -> str:
    name = os.path.basename(str(filename or "")).strip().lower()
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"\s+", " ", stem).strip()
    # Outlook / filesystem collision suffixes should not make the same document
    # appear twice in the UI: contract.pdf and contract_1.pdf are treated as the
    # same logical name when file hashing is unavailable.
    stem = re.sub(r"(?:\s*[_\-（(]?\d+[）)]?)$", "", stem).strip(" _-")
    return f"{stem}{ext}"


def _existing_file_path(value: Any) -> str:
    path = os.path.abspath(str(value or ""))
    return path if path and os.path.exists(path) and os.path.isfile(path) else ""


def _file_sha256(path: str) -> str:
    # Large Outlook .msg/.eml/PDF files can make the side panel feel sluggish if
    # every request hashes them fully.  Hash full content up to a generous limit;
    # beyond that, use a stable sampled hash based on the first/last 1 MiB plus
    # file size.  This is still strong enough for UI de-duping.
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= MAX_ATTACHMENT_HASH_BYTES:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            digest.update(fh.read(1024 * 1024))
            fh.seek(max(size - 1024 * 1024, 0))
            digest.update(fh.read(1024 * 1024))
            digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def _attachment_dedupe_key(item: Dict[str, Any], hash_cache: Dict[str, str]) -> Tuple[Any, ...]:
    filename = item.get("filename") or ""
    file_path = _existing_file_path(item.get("file_path"))
    file_size = _safe_int(item.get("file_size"), 0)
    if file_path:
        stat = os.stat(file_path)
        size = stat.st_size
        try:
            digest = hash_cache.get(file_path)
            if digest is None:
                digest = _file_sha256(file_path)
                hash_cache[file_path] = digest
            return ("content", size, digest)
        except OSError:
            pass
    return ("metadata", _normalized_attachment_name(filename), file_size)


def _read_image_dimensions(path: str, ext: str) -> Optional[Tuple[int, int]]:
    """Read basic image dimensions without adding a hard Pillow dependency."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
            if ext == ".png" and head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if ext == ".gif" and head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
                return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
            if ext == ".bmp" and head.startswith(b"BM") and len(head) >= 26:
                return int.from_bytes(head[18:22], "little", signed=True), abs(int.from_bytes(head[22:26], "little", signed=True))
            if ext in {".jpg", ".jpeg"} and head.startswith(b"\xff\xd8"):
                fh.seek(2)
                while True:
                    marker_start = fh.read(1)
                    if not marker_start:
                        return None
                    if marker_start != b"\xff":
                        continue
                    marker = fh.read(1)
                    while marker == b"\xff":
                        marker = fh.read(1)
                    if not marker or marker in {b"\xd8", b"\xd9"}:
                        continue
                    raw_len = fh.read(2)
                    if len(raw_len) != 2:
                        return None
                    block_len = int.from_bytes(raw_len, "big")
                    if block_len < 2:
                        return None
                    if marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                        data = fh.read(5)
                        if len(data) == 5:
                            return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                        return None
                    fh.seek(block_len - 2, os.SEEK_CUR)
    except Exception:
        return None
    return None


def _image_name_profile(filename: Any) -> Tuple[bool, bool]:
    base = os.path.basename(str(filename or "")).strip().lower()
    stem = os.path.splitext(base)[0]
    compact = re.sub(r"[\s_\-.()\[\]{}]+", "", stem)
    tokens = set(re.split(r"[\s_\-.()\[\]{}]+", stem))
    generic = bool(GENERIC_INLINE_IMAGE_RE.match(stem)) or bool(GENERIC_INLINE_IMAGE_RE.match(compact))
    decorative = bool(tokens & DECORATIVE_IMAGE_TOKENS) or any(token in compact for token in DECORATIVE_IMAGE_TOKENS)
    return generic, decorative


def is_probably_noise_image_attachment(item: Dict[str, Any]) -> bool:
    """Return True for email-formatting images that should not be shown.

    The filter is deliberately conservative for real evidence images. A generic
    filename alone is not enough to hide an image; size and/or dimensions must
    also indicate a signature/logo/icon/spacer. Large screenshots/photos remain
    visible even if Outlook names them image001.jpg.
    """
    filename = item.get("filename") or ""
    ext = _attachment_ext(filename)
    if ext not in IMAGE_EXTENSIONS:
        return False

    file_path = _existing_file_path(item.get("file_path"))
    size = os.path.getsize(file_path) if file_path else _safe_int(item.get("file_size"), 0)
    generic_name, decorative_name = _image_name_profile(filename)
    dimensions = _read_image_dimensions(file_path, ext) if file_path else None
    width, height = dimensions or (0, 0)
    pixels = width * height if width and height else 0

    if decorative_name and (not size or size <= DECORATIVE_IMAGE_MAX_BYTES):
        return True
    if generic_name and size and size <= TINY_IMAGE_BYTES:
        return True
    if generic_name and size and size <= GENERIC_IMAGE_SMALL_BYTES:
        return True
    if dimensions:
        longest = max(width, height)
        shortest = min(width, height)
        if longest <= 96 and shortest <= 96:
            return True
        if (generic_name or decorative_name) and longest <= 720 and pixels <= 180_000:
            return True
        if (generic_name or decorative_name) and size and size <= 220 * 1024 and longest <= 1000:
            return True
    return False


def init_db() -> sqlite3.Connection:
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            contract TEXT UNIQUE,
            name TEXT,
            client TEXT,
            amount TEXT,
            type TEXT,
            stage TEXT,
            date TEXT,
            notes TEXT,
            favorite INTEGER DEFAULT 0,
            suspended INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            archivedAt TEXT,
            archivedFromStage TEXT,
            stageDates TEXT,
            currentProgress TEXT,
            latestEmailTime TEXT,
            latestEmailSubject TEXT,
            latestSender TEXT,
            needsReview INTEGER DEFAULT 0,
            reviewReason TEXT,
            manualOverride INTEGER DEFAULT 0,
            source TEXT,
            latestAttachmentDir TEXT,
            latestEmailEntryId TEXT,
            latestEmailStoreId TEXT,
            latestEmailFolder TEXT,
            createdAt TEXT,
            updatedAt TEXT
        );

        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT UNIQUE,
            store_id TEXT,
            project_id TEXT,
            contract TEXT,
            subject TEXT,
            sender_name TEXT,
            sender_email TEXT,
            to_recipients TEXT,
            cc_recipients TEXT,
            received_time TEXT,
            sent_time TEXT,
            body TEXT,
            html_body TEXT,
            folder TEXT,
            attachment_dir TEXT,
            has_attachments INTEGER,
            is_read INTEGER,
            importance INTEGER,
            categories TEXT,
            crawled_at TEXT
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_entry_id TEXT,
            project_id TEXT,
            contract TEXT,
            filename TEXT,
            file_path TEXT,
            file_size INTEGER,
            saved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS project_aliases (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            alias_value TEXT NOT NULL,
            source_event_id TEXT,
            confidence REAL,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(alias_type, alias_value),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS project_events (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT,
            sender TEXT,
            subject TEXT,
            source_email_id TEXT,
            source_attachment_id TEXT,
            extracted_fields_json TEXT,
            confidence REAL,
            created_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS manual_overrides (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            operator TEXT,
            created_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS form_drafts (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_projects_contract ON projects(contract);
        CREATE INDEX IF NOT EXISTS idx_projects_stage ON projects(stage);
        CREATE INDEX IF NOT EXISTS idx_projects_archived ON projects(archived);
        CREATE INDEX IF NOT EXISTS idx_emails_entry_id ON emails(entry_id);
        CREATE INDEX IF NOT EXISTS idx_emails_contract_time ON emails(contract, received_time);
        CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_time);
        CREATE INDEX IF NOT EXISTS idx_emails_contract ON emails(contract);
        CREATE INDEX IF NOT EXISTS idx_project_aliases_project ON project_aliases(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_aliases_lookup ON project_aliases(alias_type, alias_value);
        CREATE INDEX IF NOT EXISTS idx_project_events_project_time ON project_events(project_id, event_time);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_project_events_email_dedupe
            ON project_events(project_id, event_type, source_email_id)
            WHERE source_email_id IS NOT NULL AND source_email_id <> '';
        """
    )
    _ensure_column(conn, "projects", "latestEmailEntryId", "TEXT")
    _ensure_column(conn, "projects", "latestEmailStoreId", "TEXT")
    _ensure_column(conn, "projects", "latestEmailFolder", "TEXT")
    _ensure_column(conn, "projects", "llmReviewed", "INTEGER DEFAULT 0")
    _ensure_column(conn, "projects", "llmSummary", "TEXT")
    _ensure_column(conn, "emails", "store_id", "TEXT")
    _ensure_column(conn, "emails", "project_id", "TEXT")
    _ensure_column(conn, "attachments", "project_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_project_time ON emails(project_id, received_time)")
    conn.commit()
    return conn


def _row_to_project(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for field in BOOL_FIELDS:
        item[field] = bool(item.get(field))
    for field in JSON_FIELDS:
        raw = item.get(field)
        if isinstance(raw, str) and raw:
            try:
                item[field] = json.loads(raw)
            except json.JSONDecodeError:
                item[field] = {}
        elif not raw:
            item[field] = {}
    return item


def list_projects(archived: Optional[bool] = None) -> List[Dict[str, Any]]:
    conn = init_db()
    try:
        if archived is None:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY archived ASC, COALESCE(latestEmailTime, date, updatedAt) DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects WHERE archived=? ORDER BY COALESCE(latestEmailTime, date, updatedAt) DESC",
                (1 if archived else 0,),
            ).fetchall()
        return [_row_to_project(row) for row in rows]
    finally:
        conn.close()


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = init_db()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return _row_to_project(row) if row else None
    finally:
        conn.close()


def get_project_by_contract(contract: str) -> Optional[Dict[str, Any]]:
    """Return a project by visible contract or any known alias.

    This is intentionally alias-aware so CQ-only opening rows and later
    M4367/SO/check rows resolve to the same project object.
    """
    if not contract:
        return None
    alias = normalize_alias(contract)
    conn = init_db()
    try:
        value = normalize_alias_value(contract)
        row = conn.execute("SELECT * FROM projects WHERE UPPER(contract)=?", (value,)).fetchone()
        if row:
            return _row_to_project(row)
        if alias:
            alias_type, alias_value = alias
            row = conn.execute(
                """
                SELECT p.*
                FROM project_aliases a
                JOIN projects p ON p.id = a.project_id
                WHERE a.alias_type=? AND a.alias_value=?
                LIMIT 1
                """,
                (alias_type, alias_value),
            ).fetchone()
            if row:
                return _row_to_project(row)
        return None
    finally:
        conn.close()


def get_project_by_any_alias(aliases: Iterable[Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Resolve aliases to one project.  Return conflicts if multiple projects match."""
    normalized_aliases = [a for a in (normalize_alias(alias) for alias in aliases) if a]
    if not normalized_aliases:
        return None, []
    conn = init_db()
    try:
        matched_ids: List[str] = []
        for alias_type, alias_value in normalized_aliases:
            rows = conn.execute(
                """
                SELECT project_id FROM project_aliases
                WHERE alias_type=? AND alias_value=?
                UNION
                SELECT id FROM projects WHERE UPPER(contract)=?
                """,
                (alias_type, alias_value, alias_value),
            ).fetchall()
            for row in rows:
                project_id = row[0]
                if project_id and project_id not in matched_ids:
                    matched_ids.append(project_id)
        if not matched_ids:
            return None, []
        row = conn.execute("SELECT * FROM projects WHERE id=?", (matched_ids[0],)).fetchone()
        conflicts = matched_ids[1:]
        return (_row_to_project(row) if row else None), conflicts
    finally:
        conn.close()


def list_project_aliases(project_id: str) -> List[Dict[str, Any]]:
    if not project_id:
        return []
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT alias_type, alias_value, confidence, created_at FROM project_aliases WHERE project_id=? ORDER BY created_at ASC",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _alias_id(alias_type: str, alias_value: str) -> str:
    raw = f"{alias_type}:{alias_value}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def save_project_alias(
    project_id: str,
    alias_type: str,
    alias_value: str,
    source_event_id: str = "",
    confidence: float = 1.0,
) -> Optional[str]:
    """Save an alias. Return conflicting project_id if the alias belongs elsewhere."""
    if not project_id:
        return None
    alias = normalize_alias({"type": alias_type, "value": alias_value})
    if not alias:
        return None
    alias_type, alias_value = alias
    conn = init_db()
    try:
        existing = conn.execute(
            "SELECT project_id FROM project_aliases WHERE alias_type=? AND alias_value=?",
            (alias_type, alias_value),
        ).fetchone()
        if existing and existing[0] != project_id:
            return str(existing[0])
        now = now_iso()
        conn.execute(
            """
            INSERT INTO project_aliases
            (id, project_id, alias_type, alias_value, source_event_id, confidence, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(alias_type, alias_value) DO UPDATE SET
                source_event_id=COALESCE(NULLIF(excluded.source_event_id, ''), project_aliases.source_event_id),
                confidence=MAX(COALESCE(project_aliases.confidence, 0), COALESCE(excluded.confidence, 0)),
                updated_at=excluded.updated_at
            """,
            (_alias_id(alias_type, alias_value), project_id, alias_type, alias_value, source_event_id, confidence, now, now),
        )
        conn.commit()
        return None
    finally:
        conn.close()


def save_project_aliases(
    project_id: str,
    aliases: Iterable[Any],
    source_event_id: str = "",
    confidence: float = 1.0,
) -> List[str]:
    conflicts: List[str] = []
    seen: set[Tuple[str, str]] = set()
    for alias in aliases:
        normalized = normalize_alias(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        conflict = save_project_alias(project_id, normalized[0], normalized[1], source_event_id=source_event_id, confidence=confidence)
        if conflict and conflict not in conflicts:
            conflicts.append(conflict)
    return conflicts


def normalize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: project.get(key, "") for key in PROJECT_COLUMNS}
    result["id"] = str(result.get("id") or result.get("contract") or "").strip()
    result["contract"] = str(result.get("contract") or result.get("id") or "").strip()
    result["name"] = str(result.get("name") or result.get("latestEmailSubject") or result.get("contract") or "").strip()
    result["client"] = str(result.get("client") or "").strip()
    result["amount"] = str(result.get("amount") or "").strip()
    result["type"] = str(result.get("type") or "standard").strip() or "standard"
    result["stage"] = str(result.get("stage") or "sales-contract").strip() or "sales-contract"
    result["date"] = str(result.get("date") or "").strip()
    result["notes"] = str(result.get("notes") or "").strip()
    result["archivedAt"] = str(result.get("archivedAt") or "").strip()
    result["archivedFromStage"] = str(result.get("archivedFromStage") or "").strip()
    result["stageDates"] = result.get("stageDates") or {}
    if not isinstance(result["stageDates"], dict):
        result["stageDates"] = {}
    for key in BOOL_FIELDS:
        result[key] = 1 if bool(result.get(key)) else 0
    result["updatedAt"] = now_iso()
    result["createdAt"] = str(result.get("createdAt") or result["updatedAt"])
    return result


def upsert_project(project: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_project(project)
    conn = init_db()
    try:
        existing = conn.execute("SELECT createdAt FROM projects WHERE id=?", (normalized["id"],)).fetchone()
        if existing and existing["createdAt"]:
            normalized["createdAt"] = existing["createdAt"]

        # Protect the UNIQUE(contract) constraint.  If another project already
        # owns the visible contract, keep the old row safe and mark this row for
        # review instead of crashing the sync.
        contract_value = normalize_alias_value(normalized.get("contract"))
        if contract_value:
            other = conn.execute(
                "SELECT id FROM projects WHERE UPPER(contract)=? AND id<>?",
                (contract_value, normalized["id"]),
            ).fetchone()
            if other:
                normalized["needsReview"] = 1
                reason = str(normalized.get("reviewReason") or "")
                conflict_reason = f"合同号 {normalized['contract']} 已属于项目 {other['id']}；已保留当前项目主键，需人工合并。"
                normalized["reviewReason"] = (reason + "；" + conflict_reason).strip("；") if reason else conflict_reason
                normalized["contract"] = normalized["id"]

        values = []
        for col in PROJECT_COLUMNS:
            value = normalized.get(col, "")
            if col in JSON_FIELDS:
                value = json.dumps(value or {}, ensure_ascii=False)
            values.append(value)
        placeholders = ",".join(["?"] * len(PROJECT_COLUMNS))
        assignments = ",".join([f"{col}=excluded.{col}" for col in PROJECT_COLUMNS if col != "id"])
        conn.execute(
            f"INSERT INTO projects ({','.join(PROJECT_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )
        conn.commit()
    finally:
        conn.close()

    saved = get_project(normalized["id"]) or normalized
    # Every project's current visible contract is also an alias.  Manual project
    # creation/editing therefore participates in later Outlook matching.
    if saved.get("contract"):
        save_project_alias(saved["id"], infer_alias_type(saved["contract"]), saved["contract"], confidence=1.0)
    return saved


def delete_project(project_id: str) -> None:
    delete_projects([project_id])


def delete_projects(project_ids: Iterable[str]) -> int:
    ids = [str(project_id) for project_id in project_ids if project_id]
    if not ids:
        return 0
    conn = init_db()
    try:
        placeholders = ",".join(["?"] * len(ids))

        # 1. Collect physical paths before deleting DB rows.
        dir_rows = conn.execute(
            f"SELECT attachment_dir FROM emails WHERE project_id IN ({placeholders}) AND attachment_dir IS NOT NULL AND attachment_dir != ''",
            ids,
        ).fetchall()
        attachment_dirs = {row[0] for row in dir_rows if row[0]}

        file_rows = conn.execute(
            f"SELECT file_path FROM attachments WHERE project_id IN ({placeholders}) AND file_path IS NOT NULL AND file_path != ''",
            ids,
        ).fetchall()
        attachment_files = {row[0] for row in file_rows if row[0]}

        # Also collect latestAttachmentDir from projects table.
        project_dir_rows = conn.execute(
            f"SELECT latestAttachmentDir FROM projects WHERE id IN ({placeholders}) AND latestAttachmentDir IS NOT NULL AND latestAttachmentDir != ''",
            ids,
        ).fetchall()
        for row in project_dir_rows:
            if row[0]:
                attachment_dirs.add(row[0])

        existing_project_count = conn.execute(
            f"SELECT COUNT(*) FROM projects WHERE id IN ({placeholders})",
            ids,
        ).fetchone()[0]

        # 2. Delete all related DB records.
        conn.executemany("DELETE FROM attachments WHERE project_id=?", [(pid,) for pid in ids])
        conn.executemany("DELETE FROM emails WHERE project_id=?", [(pid,) for pid in ids])
        conn.executemany("DELETE FROM project_aliases WHERE project_id=?", [(pid,) for pid in ids])
        conn.executemany("DELETE FROM project_events WHERE project_id=?", [(pid,) for pid in ids])
        conn.executemany("DELETE FROM manual_overrides WHERE project_id=?", [(pid,) for pid in ids])
        conn.executemany("DELETE FROM projects WHERE id=?", [(pid,) for pid in ids])
        conn.commit()

        # 3. Delete physical files/directories on disk (safe: only within attachments root).
        ATTACHMENT_ROOT = os.path.abspath(os.path.join(DATA_DIR, "attachments"))
        for dir_path in attachment_dirs:
            try:
                abs_dir = os.path.abspath(dir_path)
                if abs_dir.startswith(ATTACHMENT_ROOT + os.sep) and os.path.isdir(abs_dir):
                    shutil.rmtree(abs_dir, ignore_errors=True)
            except Exception:
                pass
        for file_path in attachment_files:
            try:
                abs_file = os.path.abspath(file_path)
                if abs_file.startswith(ATTACHMENT_ROOT + os.sep) and os.path.isfile(abs_file):
                    os.unlink(abs_file)
            except Exception:
                pass

        return int(existing_project_count or 0)
    finally:
        conn.close()


def replace_projects(projects: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized_projects = [normalize_project(project) for project in projects]
    conn = init_db()
    try:
        conn.execute("DELETE FROM attachments")
        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM project_aliases")
        conn.execute("DELETE FROM project_events")
        conn.execute("DELETE FROM manual_overrides")
        conn.execute("DELETE FROM projects")
        placeholders = ",".join(["?"] * len(PROJECT_COLUMNS))
        for normalized in normalized_projects:
            values = []
            for col in PROJECT_COLUMNS:
                value = normalized.get(col, "")
                if col in JSON_FIELDS:
                    value = json.dumps(value or {}, ensure_ascii=False)
                values.append(value)
            conn.execute(
                f"INSERT INTO projects ({','.join(PROJECT_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
        conn.commit()
    finally:
        conn.close()
    for normalized in normalized_projects:
        if normalized.get("contract"):
            save_project_alias(normalized["id"], infer_alias_type(normalized["contract"]), normalized["contract"], confidence=1.0)
    return list_projects()


def _event_id(project_id: str, event_type: str, source_email_id: str, subject: str, event_time: str) -> str:
    raw = "|".join([project_id or "", event_type or "", source_email_id or "", subject or "", event_time or ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def record_project_event(
    project_id: str,
    event_type: str,
    event_time: Any = "",
    sender: str = "",
    subject: str = "",
    source_email_id: str = "",
    source_attachment_id: str = "",
    extracted_fields: Optional[Dict[str, Any]] = None,
    confidence: float = 0.0,
) -> str:
    if not project_id:
        return ""
    event_time_text = event_time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(event_time, datetime) else str(event_time or "")
    event_type = str(event_type or "UNKNOWN")
    source_email_id = str(source_email_id or "")
    subject = str(subject or "")
    event_id = _event_id(project_id, event_type, source_email_id, subject, event_time_text)
    conn = init_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO project_events
            (id, project_id, event_type, event_time, sender, subject, source_email_id,
             source_attachment_id, extracted_fields_json, confidence, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                project_id,
                event_type,
                event_time_text,
                sender,
                subject,
                source_email_id,
                source_attachment_id,
                json.dumps(extracted_fields or {}, ensure_ascii=False),
                confidence,
                now_iso(),
            ),
        )
        conn.commit()
        return event_id
    finally:
        conn.close()


def record_manual_override(
    project_id: str,
    field_name: str,
    old_value: Any,
    new_value: Any,
    reason: str = "",
    operator: str = "",
) -> str:
    if not project_id or not field_name:
        return ""
    raw = f"{project_id}|{field_name}|{old_value}|{new_value}|{now_iso()}"
    override_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    conn = init_db()
    try:
        conn.execute(
            """
            INSERT INTO manual_overrides
            (id, project_id, field_name, old_value, new_value, reason, operator, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (override_id, project_id, field_name, str(old_value or ""), str(new_value or ""), reason, operator, now_iso()),
        )
        conn.commit()
        return override_id
    finally:
        conn.close()


def save_email_record(email: Dict[str, Any]) -> None:
    conn = init_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO emails
            (entry_id, store_id, project_id, contract, subject, sender_name, sender_email, to_recipients, cc_recipients,
             received_time, sent_time, body, html_body, folder, attachment_dir, has_attachments,
             is_read, importance, categories, crawled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                email.get("entry_id"), email.get("store_id"), email.get("project_id"), email.get("contract"), email.get("subject"),
                email.get("sender_name"), email.get("sender_email"), email.get("to_recipients"),
                email.get("cc_recipients"), email.get("received_time"), email.get("sent_time"),
                email.get("body"), email.get("html_body"), email.get("folder"),
                email.get("attachment_dir"), 1 if email.get("has_attachments") else 0,
                1 if email.get("is_read") else 0, email.get("importance"), email.get("categories"),
                email.get("crawled_at") or now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_email_for_contract(contract: str) -> Optional[Dict[str, Any]]:
    if not contract:
        return None
    project = get_project_by_contract(contract)
    conn = init_db()
    try:
        if project:
            row = conn.execute(
                """
                SELECT entry_id, store_id, subject, folder, received_time, sent_time
                FROM emails
                WHERE project_id=? AND COALESCE(entry_id, '') <> ''
                ORDER BY COALESCE(received_time, sent_time, crawled_at) DESC
                LIMIT 1
                """,
                (project["id"],),
            ).fetchone()
            if row:
                return dict(row)
        row = conn.execute(
            """
            SELECT entry_id, store_id, subject, folder, received_time, sent_time
            FROM emails
            WHERE contract=? AND COALESCE(entry_id, '') <> ''
            ORDER BY COALESCE(received_time, sent_time, crawled_at) DESC
            LIMIT 1
            """,
            (contract,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()



def list_project_attachments(project_id: str) -> List[Dict[str, Any]]:
    """Return every saved attachment that belongs to one project object.

    Attachments are persisted from all classified Outlook mails mapped to the
    project, not only the latest mail.  The query keeps email metadata beside the
    file record so the front end can show where each attachment came from.
    """
    if not project_id:
        return []
    conn = init_db()
    try:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.email_entry_id,
                a.project_id,
                a.contract,
                a.filename,
                a.file_path,
                a.file_size,
                a.saved_at,
                e.subject AS email_subject,
                e.sender_name AS email_sender,
                e.sender_email AS email_sender_email,
                e.received_time AS email_received_time,
                e.sent_time AS email_sent_time,
                e.folder AS email_folder
            FROM attachments a
            LEFT JOIN emails e ON e.entry_id = a.email_entry_id
            WHERE a.project_id=?
            ORDER BY COALESCE(e.received_time, e.sent_time, a.saved_at) DESC,
                     a.filename COLLATE NOCASE ASC,
                     a.id DESC
            """,
            (project_id,),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        seen_keys: set[Tuple[Any, ...]] = set()
        hash_cache: Dict[str, str] = {}
        for row in rows:
            item = dict(row)
            filename = item.get("filename") or ""
            is_image = is_image_attachment(filename)

            # Non-image files: only de-duplicate. Images: first suppress obvious
            # Outlook/signature/template noise, then de-duplicate the remaining
            # real image attachments.
            if is_image and is_probably_noise_image_attachment(item):
                continue

            dedupe_key = _attachment_dedupe_key(item, hash_cache)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            result.append({
                "id": item.get("id"),
                "emailEntryId": item.get("email_entry_id") or "",
                "projectId": item.get("project_id") or "",
                "contract": item.get("contract") or "",
                "filename": filename,
                "filePath": item.get("file_path") or "",
                "fileSize": item.get("file_size") or 0,
                "savedAt": item.get("saved_at") or "",
                "emailSubject": item.get("email_subject") or "",
                "emailSender": item.get("email_sender") or item.get("email_sender_email") or "",
                "emailTime": item.get("email_received_time") or item.get("email_sent_time") or item.get("saved_at") or "",
                "emailFolder": item.get("email_folder") or "",
                "exists": os.path.exists(str(item.get("file_path") or "")),
                "isImage": is_image,
            })
        return result
    finally:
        conn.close()


def get_attachment(attachment_id: Any) -> Optional[Dict[str, Any]]:
    """Return one attachment row by database id."""
    try:
        numeric_id = int(attachment_id)
    except (TypeError, ValueError):
        return None
    conn = init_db()
    try:
        row = conn.execute(
            """
            SELECT id, email_entry_id, project_id, contract, filename, file_path, file_size, saved_at
            FROM attachments
            WHERE id=?
            LIMIT 1
            """,
            (numeric_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def save_attachment_records(records: Iterable[Dict[str, Any]]) -> None:
    conn = init_db()
    try:
        conn.executemany(
            """
            INSERT INTO attachments (email_entry_id, project_id, contract, filename, file_path, file_size, saved_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                (
                    r.get("email_entry_id"), r.get("project_id"), r.get("contract"), r.get("filename"),
                    r.get("file_path"), r.get("file_size"), r.get("saved_at") or now_iso(),
                )
                for r in records
            ],
        )
        conn.commit()
    finally:
        conn.close()


def save_form_draft(key: str, value: str) -> None:
    """Persist a form draft (e.g. new-project form) so it survives browser restarts."""
    if not key:
        return
    now = now_iso()
    conn = init_db()
    try:
        existing = conn.execute("SELECT created_at FROM form_drafts WHERE key=?", (key,)).fetchone()
        created_at = existing["created_at"] if existing and existing["created_at"] else now
        conn.execute(
            """
            INSERT INTO form_drafts (key, value, created_at, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, created_at, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_form_draft(key: str) -> Optional[str]:
    """Return a previously saved form draft value, or None."""
    if not key:
        return None
    conn = init_db()
    try:
        row = conn.execute("SELECT value FROM form_drafts WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def delete_form_draft(key: str) -> None:
    """Remove a form draft after it has been consumed (project created)."""
    if not key:
        return
    conn = init_db()
    try:
        conn.execute("DELETE FROM form_drafts WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()
