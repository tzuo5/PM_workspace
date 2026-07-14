# -*- coding: utf-8 -*-
"""SQLite persistence for Document Check feature.

Tables:
- review_cases
- review_documents
- evidence_refs
- extracted_fields
- check_items
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import project_db as pdb

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.environ.get("PM_TRACKER_DB", os.path.join(DATA_DIR, "pm_tracker.db"))

DOCUMENT_CHECK_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS review_cases (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT DEFAULT 'UPLOADING',
    overall_conclusion TEXT,
    review_version INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS review_documents (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    original_filename TEXT,
    stored_filename TEXT,
    sha256 TEXT,
    workspace TEXT DEFAULT 'A',
    detected_type TEXT,
    manual_type TEXT,
    page_count INTEGER DEFAULT 0,
    embedded_sections TEXT,
    parse_status TEXT DEFAULT 'pending',
    parse_error TEXT,
    file_size INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(case_id) REFERENCES review_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    id TEXT PRIMARY KEY,
    case_id TEXT,
    document_id TEXT,
    document_type TEXT,
    page_number INTEGER DEFAULT 1,
    bbox_json TEXT,
    raw_text TEXT,
    normalized_text TEXT,
    extraction_method TEXT DEFAULT 'PDF_TEXT',
    confidence REAL DEFAULT 0.0,
    created_at TEXT,
    FOREIGN KEY(document_id) REFERENCES review_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES review_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id TEXT PRIMARY KEY,
    case_id TEXT,
    document_id TEXT,
    field_name TEXT NOT NULL,
    value TEXT,
    normalized_value TEXT,
    confidence REAL DEFAULT 0.0,
    evidence_refs TEXT,
    manual_override TEXT,
    original_value TEXT,
    override_time TEXT,
    override_source TEXT,
    override_note TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(case_id) REFERENCES review_cases(id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES review_documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS check_items (
    id TEXT PRIMARY KEY,
    case_id TEXT,
    rule_id TEXT NOT NULL,
    category TEXT,
    label TEXT,
    status TEXT DEFAULT 'UNKNOWN',
    is_blocker INTEGER DEFAULT 0,
    summary TEXT,
    details TEXT,
    values_json TEXT,
    evidence_refs TEXT,
    confidence REAL DEFAULT 0.0,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(case_id) REFERENCES review_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_review_docs_case ON review_documents(case_id);
CREATE INDEX IF NOT EXISTS idx_review_docs_sha256 ON review_documents(sha256);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence_refs(case_id);
CREATE INDEX IF NOT EXISTS idx_evidence_doc ON evidence_refs(document_id);
CREATE INDEX IF NOT EXISTS idx_extracted_case ON extracted_fields(case_id);
CREATE INDEX IF NOT EXISTS idx_extracted_doc ON extracted_fields(document_id);
CREATE INDEX IF NOT EXISTS idx_check_items_case ON check_items(case_id);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _uid(prefix: str = "") -> str:
    import uuid
    return prefix + uuid.uuid4().hex[:12]


def init_dc_db() -> sqlite3.Connection:
    """Initialize Document Check tables."""
    conn = pdb.init_db()
    conn.executescript(DOCUMENT_CHECK_TABLES_SQL)
    conn.commit()
    return conn


# --- ReviewCase ---

def create_review_case(name: str = "") -> Dict[str, Any]:
    conn = init_dc_db()
    case_id = _uid("case_")
    now = _now()
    try:
        conn.execute(
            "INSERT INTO review_cases (id, name, status, overall_conclusion, review_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (case_id, name or f"Case {now}", "UPLOADING", None, 1, now, now),
        )
        conn.commit()
        return get_review_case(case_id) or {"id": case_id}
    finally:
        conn.close()


def get_review_case(case_id: str) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        row = conn.execute("SELECT * FROM review_cases WHERE id=?", (case_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_review_cases() -> List[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        rows = conn.execute("SELECT * FROM review_cases ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_review_case(case_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        allowed = {"name", "status", "overall_conclusion", "review_version"}
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return get_review_case(case_id)
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [case_id]
        conn.execute(f"UPDATE review_cases SET {set_clause} WHERE id=?", values)
        conn.commit()
        return get_review_case(case_id)
    finally:
        conn.close()


def delete_review_case(case_id: str) -> None:
    conn = init_dc_db()
    try:
        # Get document stored filenames first
        docs = conn.execute("SELECT stored_filename FROM review_documents WHERE case_id=?", (case_id,)).fetchall()
        # Cascade delete DB rows
        conn.execute("DELETE FROM check_items WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM extracted_fields WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM evidence_refs WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM review_documents WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM review_cases WHERE id=?", (case_id,))
        conn.commit()
        # Delete physical files
        attach_root = os.path.abspath(os.path.join(DATA_DIR, "attachments"))
        for doc in docs:
            fname = doc["stored_filename"]
            if fname:
                dc_dir = os.path.join(attach_root, "document_check")
                file_path = os.path.join(dc_dir, fname)
                abs_path = os.path.abspath(file_path)
                if abs_path.startswith(attach_root + os.sep) and os.path.isfile(abs_path):
                    try:
                        os.unlink(abs_path)
                    except Exception:
                        pass
    finally:
        conn.close()


# --- ReviewDocument ---

def add_review_document(case_id: str, original_filename: str, stored_filename: str,
                        sha256: str, workspace: str = "A", file_size: int = 0) -> Dict[str, Any]:
    conn = init_dc_db()
    doc_id = _uid("doc_")
    now = _now()
    try:
        conn.execute(
            """INSERT INTO review_documents
            (id, case_id, original_filename, stored_filename, sha256, workspace,
             detected_type, manual_type, page_count, embedded_sections, parse_status, parse_error, file_size, created_at, updated_at)
            VALUES (?,?,?,?,?,?,NULL,NULL,0,'[]','pending',NULL,?,?,?)""",
            (doc_id, case_id, original_filename, stored_filename, sha256, workspace, file_size, now, now),
        )
        conn.commit()
        return get_review_document(doc_id) or {"id": doc_id}
    finally:
        conn.close()


def get_review_document(doc_id: str) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        row = conn.execute("SELECT * FROM review_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["embedded_sections"] = json.loads(d.get("embedded_sections") or "[]")
        return d
    finally:
        conn.close()


def list_review_documents(case_id: str) -> List[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        rows = conn.execute(
            "SELECT * FROM review_documents WHERE case_id=? ORDER BY workspace, created_at",
            (case_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["embedded_sections"] = json.loads(d.get("embedded_sections") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def update_review_document(doc_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        allowed = {"detected_type", "manual_type", "page_count", "embedded_sections",
                   "parse_status", "parse_error", "workspace"}
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return get_review_document(doc_id)
        if "embedded_sections" in fields and isinstance(fields["embedded_sections"], (list, dict)):
            fields["embedded_sections"] = json.dumps(fields["embedded_sections"], ensure_ascii=False)
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [doc_id]
        conn.execute(f"UPDATE review_documents SET {set_clause} WHERE id=?", values)
        conn.commit()
        return get_review_document(doc_id)
    finally:
        conn.close()


def delete_review_document(doc_id: str) -> None:
    conn = init_dc_db()
    try:
        doc = conn.execute("SELECT stored_filename FROM review_documents WHERE id=?", (doc_id,)).fetchone()
        conn.execute("DELETE FROM evidence_refs WHERE document_id=?", (doc_id,))
        conn.execute("DELETE FROM extracted_fields WHERE document_id=?", (doc_id,))
        conn.execute("DELETE FROM review_documents WHERE id=?", (doc_id,))
        conn.commit()
        if doc and doc["stored_filename"]:
            attach_root = os.path.abspath(os.path.join(DATA_DIR, "attachments"))
            file_path = os.path.join(attach_root, "document_check", doc["stored_filename"])
            abs_path = os.path.abspath(file_path)
            if abs_path.startswith(attach_root + os.sep) and os.path.isfile(abs_path):
                try:
                    os.unlink(abs_path)
                except Exception:
                    pass
    finally:
        conn.close()


def find_document_by_sha256(case_id: str, sha256: str) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        row = conn.execute(
            "SELECT * FROM review_documents WHERE case_id=? AND sha256=?",
            (case_id, sha256),
        ).fetchone()
        if row:
            d = dict(row)
            d["embedded_sections"] = json.loads(d.get("embedded_sections") or "[]")
            return d
        return None
    finally:
        conn.close()


# --- EvidenceRef ---

def add_evidence(case_id: str, document_id: str, page_number: int, bbox: Dict[str, float],
                 raw_text: str, normalized_text: str = "", extraction_method: str = "PDF_TEXT",
                 confidence: float = 0.0, document_type: str = "") -> str:
    conn = init_dc_db()
    ev_id = _uid("ev_")
    now = _now()
    try:
        conn.execute(
            """INSERT INTO evidence_refs
            (id, case_id, document_id, document_type, page_number, bbox_json,
             raw_text, normalized_text, extraction_method, confidence, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ev_id, case_id, document_id, document_type, page_number,
             json.dumps(bbox, ensure_ascii=False), raw_text, normalized_text,
             extraction_method, confidence, now),
        )
        conn.commit()
        return ev_id
    finally:
        conn.close()


def list_evidence_for_document(document_id: str) -> List[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        rows = conn.execute(
            "SELECT * FROM evidence_refs WHERE document_id=? ORDER BY page_number",
            (document_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["bbox"] = json.loads(d.get("bbox_json") or "{}")
            del d["bbox_json"]
            result.append(d)
        return result
    finally:
        conn.close()


def get_evidence_by_ids(ev_ids: List[str]) -> List[Dict[str, Any]]:
    if not ev_ids:
        return []
    conn = init_dc_db()
    try:
        placeholders = ",".join(["?"] * len(ev_ids))
        rows = conn.execute(
            f"SELECT * FROM evidence_refs WHERE id IN ({placeholders}) ORDER BY page_number",
            ev_ids,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["bbox"] = json.loads(d.get("bbox_json") or "{}")
            del d["bbox_json"]
            result.append(d)
        return result
    finally:
        conn.close()


# --- ExtractedField ---

def upsert_extracted_field(case_id: str, document_id: str, field_name: str,
                           value: str, normalized_value: str = "",
                           confidence: float = 0.0, evidence_refs: List[str] = None) -> str:
    conn = init_dc_db()
    field_id = _uid("fld_")
    now = _now()
    ev_json = json.dumps(evidence_refs or [], ensure_ascii=False)
    try:
        # Check if field already exists for this document+field_name
        existing = conn.execute(
            "SELECT id FROM extracted_fields WHERE document_id=? AND field_name=?",
            (document_id, field_name),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE extracted_fields SET value=?, normalized_value=?, confidence=?,
                evidence_refs=?, updated_at=? WHERE id=?""",
                (value, normalized_value, confidence, ev_json, now, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO extracted_fields
                (id, case_id, document_id, field_name, value, normalized_value, confidence,
                 evidence_refs, manual_override, original_value, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?)""",
                (field_id, case_id, document_id, field_name, value, normalized_value, confidence,
                 ev_json, now, now),
            )
        conn.commit()
        return existing["id"] if existing else field_id
    finally:
        conn.close()


def list_extracted_fields(case_id: str) -> List[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        rows = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id=? ORDER BY field_name",
            (case_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
            d["manual_override"] = json.loads(d.get("manual_override") or "{}") if d.get("manual_override") else None
            result.append(d)
        return result
    finally:
        conn.close()


def get_extracted_field(case_id: str, field_name: str) -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        row = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id=? AND field_name=? ORDER BY confidence DESC LIMIT 1",
            (case_id, field_name),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
        d["manual_override"] = json.loads(d.get("manual_override") or "{}") if d.get("manual_override") else None
        return d
    finally:
        conn.close()


def apply_manual_override(field_id: str, new_value: str, note: str = "",
                          source: str = "manual") -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    now = _now()
    try:
        row = conn.execute("SELECT * FROM extracted_fields WHERE id=?", (field_id,)).fetchone()
        if not row:
            return None
        old = dict(row)
        override = {
            "original_value": old.get("value"),
            "new_value": new_value,
            "time": now,
            "source": source,
            "note": note,
        }
        conn.execute(
            "UPDATE extracted_fields SET value=?, manual_override=?, updated_at=? WHERE id=?",
            (new_value, json.dumps(override, ensure_ascii=False), now, field_id),
        )
        conn.commit()
        d = dict(conn.execute("SELECT * FROM extracted_fields WHERE id=?", (field_id,)).fetchone())
        d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
        d["manual_override"] = json.loads(d.get("manual_override") or "{}") if d.get("manual_override") else None
        return d
    finally:
        conn.close()


# --- CheckItem ---

def upsert_check_item(case_id: str, rule_id: str, category: str, label: str,
                      status: str = "UNKNOWN", is_blocker: bool = False,
                      summary: str = "", details: str = "",
                      values: Dict[str, str] = None,
                      evidence_refs: List[str] = None,
                      confidence: float = 0.0) -> str:
    conn = init_dc_db()
    now = _now()
    values_json = json.dumps(values or {}, ensure_ascii=False)
    ev_json = json.dumps(evidence_refs or [], ensure_ascii=False)
    try:
        existing = conn.execute(
            "SELECT id FROM check_items WHERE case_id=? AND rule_id=?",
            (case_id, rule_id),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE check_items SET category=?, label=?, status=?, is_blocker=?,
                summary=?, details=?, values_json=?, evidence_refs=?, confidence=?, updated_at=?
                WHERE id=?""",
                (category, label, status, 1 if is_blocker else 0, summary, details,
                 values_json, ev_json, confidence, now, existing["id"]),
            )
            return existing["id"]
        else:
            item_id = _uid("chk_")
            conn.execute(
                """INSERT INTO check_items
                (id, case_id, rule_id, category, label, status, is_blocker,
                 summary, details, values_json, evidence_refs, confidence, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, case_id, rule_id, category, label, status, 1 if is_blocker else 0,
                 summary, details, values_json, ev_json, confidence, now, now),
            )
            return item_id
    finally:
        conn.commit()
        conn.close()


def list_check_items(case_id: str) -> List[Dict[str, Any]]:
    conn = init_dc_db()
    try:
        rows = conn.execute(
            "SELECT * FROM check_items WHERE case_id=? ORDER BY is_blocker DESC, category, rule_id",
            (case_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_blocker"] = bool(d.get("is_blocker"))
            d["values"] = json.loads(d.get("values_json") or "{}")
            del d["values_json"]
            d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def update_check_item_status(item_id: str, status: str, summary: str = "",
                             details: str = "") -> Optional[Dict[str, Any]]:
    conn = init_dc_db()
    now = _now()
    try:
        conn.execute(
            "UPDATE check_items SET status=?, summary=COALESCE(NULLIF(?, ''), summary), details=COALESCE(NULLIF(?, ''), details), updated_at=? WHERE id=?",
            (status, summary, details, now, item_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM check_items WHERE id=?", (item_id,)).fetchone()
        if row:
            d = dict(row)
            d["is_blocker"] = bool(d.get("is_blocker"))
            d["values"] = json.loads(d.get("values_json") or "{}")
            del d["values_json"]
            d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
            return d
        return None
    finally:
        conn.close()