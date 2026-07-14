# -*- coding: utf-8 -*-
"""Local backend for PM Workplace.

Run:
    python start_pm_workplace.py

The server intentionally does not touch Outlook on startup. Outlook sync starts
only after the front end calls POST /api/outlook/sync.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services import project_db  # noqa: E402
from services.outlook_sync import SyncCancelled, default_date_range, sync_outlook  # noqa: E402
from services import document_check_db as dcdb  # noqa: E402
from services import document_check_service as dcsvc  # noqa: E402

project_db.init_db()

SYNC_JOBS: Dict[str, Dict[str, Any]] = {}
SYNC_CANCEL_EVENTS: Dict[str, threading.Event] = {}
SYNC_LOCK = threading.Lock()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def job_log(job_id: str, message: str) -> None:
    with SYNC_LOCK:
        job = SYNC_JOBS.setdefault(job_id, {})
        job.setdefault("logs", []).append({"time": now_text(), "message": message})
        job["updatedAt"] = now_text()


def run_sync_job(job_id: str, payload: Dict[str, Any]) -> None:
    with SYNC_LOCK:
        SYNC_JOBS[job_id].update({"status": "running", "startedAt": now_text(), "updatedAt": now_text()})

    pythoncom = None
    try:
        try:
            import pythoncom as _pythoncom  # type: ignore
            pythoncom = _pythoncom
            pythoncom.CoInitialize()
            job_log(job_id, "Windows COM 初始化完成。")
        except ImportError:
            pythoncom = None

        cancel_event = SYNC_CANCEL_EVENTS.get(job_id)
        result = sync_outlook(
            payload,
            log=lambda message: job_log(job_id, message),
            cancel_check=lambda: bool(cancel_event and cancel_event.is_set()),
        )
        with SYNC_LOCK:
            SYNC_JOBS[job_id].update({
                "status": "completed",
                "finishedAt": now_text(),
                "updatedAt": now_text(),
                "result": result,
            })
    except SyncCancelled as exc:
        job_log(job_id, "已取消，本次无数据写入。")
        with SYNC_LOCK:
            SYNC_JOBS[job_id].update({
                "status": "cancelled",
                "finishedAt": now_text(),
                "updatedAt": now_text(),
                "error": "",
                "result": {"ok": True, "cancelled": True, "message": str(exc), "counts": {"created": 0, "updated": 0, "review": 0}},
            })
    except Exception as exc:
        job_log(job_id, f"同步失败：{exc}")
        with SYNC_LOCK:
            SYNC_JOBS[job_id].update({
                "status": "failed",
                "finishedAt": now_text(),
                "updatedAt": now_text(),
                "error": str(exc),
                "result": {"ok": False, "message": str(exc)},
            })
    finally:
        with SYNC_LOCK:
            SYNC_CANCEL_EVENTS.pop(job_id, None)
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def safe_static_path(path: str) -> Optional[str]:
    parsed_path = unquote(path.split("?", 1)[0])
    if parsed_path == "/":
        parsed_path = "/index.html"
    rel_path = parsed_path.lstrip("/")
    full_path = os.path.abspath(os.path.join(FRONTEND_DIR, rel_path))
    frontend_root = os.path.abspath(FRONTEND_DIR)
    if not full_path.startswith(frontend_root + os.sep) and full_path != frontend_root:
        return None
    if os.path.isdir(full_path):
        full_path = os.path.join(full_path, "index.html")
    return full_path if os.path.exists(full_path) else None


def open_email_in_outlook(payload: Dict[str, Any]) -> Dict[str, Any]:
    entry_id = str(payload.get("entry_id") or payload.get("entryId") or "").strip()
    store_id = str(payload.get("store_id") or payload.get("storeId") or "").strip()
    contract = str(payload.get("contract") or "").strip()
    project_id = str(payload.get("project_id") or payload.get("projectId") or "").strip()

    if not entry_id and project_id:
        project = project_db.get_project(project_id)
        if project:
            entry_id = str(project.get("latestEmailEntryId") or "").strip()
            store_id = str(project.get("latestEmailStoreId") or "").strip()
            contract = contract or str(project.get("contract") or "").strip()

    if not entry_id and contract:
        latest_email = project_db.get_latest_email_for_contract(contract)
        if latest_email:
            entry_id = str(latest_email.get("entry_id") or "").strip()
            store_id = str(latest_email.get("store_id") or "").strip()

    if not entry_id:
        raise ValueError("当前项目没有可打开的 Outlook 原始邮件记录。请先重新同步 Outlook。")

    pythoncom = None
    try:
        import pythoncom as _pythoncom  # type: ignore
        import win32com.client  # type: ignore
        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")
        try:
            item = namespace.GetItemFromID(entry_id, store_id) if store_id else namespace.GetItemFromID(entry_id)
        except Exception:
            item = namespace.GetItemFromID(entry_id)
        item.Display(False)
        return {"ok": True, "message": "已在 Outlook 中打开原始邮件。"}
    except ImportError:
        raise RuntimeError("当前环境缺少 pywin32，无法控制传统 Outlook。请运行：pip install pywin32")
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def open_attachment_file(payload: Dict[str, Any]) -> Dict[str, Any]:
    attachment_id = payload.get("attachmentId") or payload.get("attachment_id") or payload.get("id")
    project_id = str(payload.get("projectId") or payload.get("project_id") or "").strip()
    attachment = project_db.get_attachment(attachment_id)
    if not attachment:
        raise ValueError("未找到该附件记录。请先重新同步 Outlook。")
    if project_id and str(attachment.get("project_id") or "") != project_id:
        raise ValueError("该附件不属于当前项目，已拒绝打开。")

    file_path = os.path.abspath(str(attachment.get("file_path") or ""))
    if not file_path:
        raise ValueError("附件路径为空。请先重新同步 Outlook。")
    if not os.path.exists(file_path):
        raise FileNotFoundError("本地附件文件不存在。可能已被移动、删除，或需要重新同步 Outlook。")

    if sys.platform.startswith("win"):
        os.startfile(file_path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", file_path])
    else:
        subprocess.Popen(["xdg-open", file_path])
    return {"ok": True, "message": f"已打开附件：{os.path.basename(file_path)}"}


class PMRequestHandler(BaseHTTPRequestHandler):
    server_version = "PMWorkplace/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_headers(self, status: int = 200, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._send_headers(status=status)
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"ok": False, "error": message}, status=status)

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _parse_multipart(self, boundary: str) -> Tuple[bytes, str]:
        """Simple multipart form-data parser for file uploads."""
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length)
        boundary_bytes = boundary.encode("utf-8")
        parts = raw.split(b"--" + boundary_bytes)

        file_content = b""
        filename = "upload.pdf"
        for part in parts:
            if b"Content-Disposition" in part and b"filename=" in part:
                import re as _re
                header, content = part.split(b"\r\n\r\n", 1)
                header_str = header.decode("utf-8", errors="replace")
                for line in header_str.split("\r\n"):
                    if "filename=" in line:
                        match = _re.search(r'filename="([^"]*)"', line)
                        if match:
                            filename = match.group(1)
                        break
                content = content.rsplit(b"\r\n--", 1)[0]
                content = content.rstrip(b"\r\n")
                file_content = content
                break
        return file_content, filename

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_headers(status=HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "time": now_text(), "defaultDateRange": default_date_range()})
                return
            if path.startswith("/api/drafts/"):
                draft_key = unquote(path.rsplit("/", 1)[-1])
                draft_value = project_db.get_form_draft(draft_key)
                self.send_json({"ok": True, "key": draft_key, "value": draft_value})
                return
            if path == "/api/projects":
                query = parse_qs(parsed.query)
                archived_arg = (query.get("archived") or [None])[0]
                archived = None
                if archived_arg is not None:
                    archived = archived_arg.lower() in {"1", "true", "yes", "y"}
                self.send_json({"ok": True, "projects": project_db.list_projects(archived=archived)})
                return
            if path.startswith("/api/projects/") and path.endswith("/attachments"):
                parts = path.strip("/").split("/")
                project_id = unquote(parts[2]) if len(parts) >= 4 else ""
                if not project_id:
                    self.send_error_json("缺少项目 ID。", status=400)
                    return
                self.send_json({"ok": True, "attachments": project_db.list_project_attachments(project_id)})
                return
            if path.startswith("/api/outlook/sync/"):
                job_id = path.rsplit("/", 1)[-1]
                with SYNC_LOCK:
                    job = SYNC_JOBS.get(job_id)
                    if not job:
                        self.send_error_json("未找到同步任务。", status=404)
                        return
                    self.send_json({"ok": True, "job": job})
                return

            # Document Check GET endpoints
            if path == "/api/document-check/cases":
                cases = dcdb.list_review_cases()
                self.send_json({"ok": True, "cases": cases})
                return

            if path.startswith("/api/document-check/cases/") and path.endswith("/status"):
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                status = dcsvc.get_review_job_status(case_id) if case_id else {"status": "unknown"}
                self.send_json({"ok": True, "status": status})
                return

            if path.startswith("/api/document-check/cases/") and path.endswith("/results"):
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                case = dcdb.get_review_case(case_id)
                docs = dcdb.list_review_documents(case_id)
                fields = dcdb.list_extracted_fields(case_id)
                items = dcdb.list_check_items(case_id)
                # Resolve evidence data for each check item
                all_ev_ids = []
                for item in items:
                    for ev_id in (item.get("evidence_refs") or []):
                        if ev_id not in all_ev_ids:
                            all_ev_ids.append(ev_id)
                evidence_map = {}
                if all_ev_ids:
                    ev_rows = dcdb.get_evidence_by_ids(all_ev_ids)
                    for ev in ev_rows:
                        evidence_map[ev["id"]] = ev
                self.send_json({"ok": True, "case": case, "documents": docs,
                                "extracted_fields": fields, "check_items": items,
                                "evidence": evidence_map})
                return

            if path.startswith("/api/document-check/cases/") and path.count("/") == 4:
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                case = dcdb.get_review_case(case_id)
                if not case:
                    self.send_error_json("Case 不存在。", status=404)
                    return
                docs = dcdb.list_review_documents(case_id)
                self.send_json({"ok": True, "case": case, "documents": docs})
                return

            if path.startswith("/api/document-check/documents/") and path.endswith("/file"):
                parts = [p for p in path.strip("/").split("/") if p]
                doc_id = parts[3] if len(parts) >= 4 else ""
                if not doc_id:
                    self.send_error_json("缺少 document ID。", status=400)
                    return
                doc = dcdb.get_review_document(doc_id)
                if not doc:
                    self.send_error_json("文档不存在。", status=404)
                    return
                dc_dir = os.path.join(BACKEND_DIR, "data", "attachments", "document_check")
                filepath = os.path.join(dc_dir, doc.get("stored_filename", ""))
                if not os.path.isfile(filepath):
                    self.send_error_json("文件不存在于磁盘。", status=404)
                    return
                with open(filepath, "rb") as fh:
                    data = fh.read()
                self._send_headers(status=200, content_type="application/pdf")
                self.wfile.write(data)
                return

            if path.startswith("/api/document-check/documents/") and path.count("/") == 4:
                parts = [p for p in path.strip("/").split("/") if p]
                doc_id = parts[3] if len(parts) >= 4 else ""
                if not doc_id:
                    self.send_error_json("缺少 document ID。", status=400)
                    return
                doc = dcdb.get_review_document(doc_id)
                if not doc:
                    self.send_error_json("文档不存在。", status=404)
                    return
                self.send_json({"ok": True, "document": doc})
                return

            if path.startswith("/api/"):
                self.send_error_json("未知 API。", status=404)
                return
            self.serve_static(path)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self.read_json()
            if path == "/api/projects":
                payload["manualOverride"] = True
                project = project_db.upsert_project(payload)
                self.send_json({"ok": True, "project": project})
                return
            if path == "/api/drafts":
                draft_key = str(payload.get("key") or "").strip()
                draft_value = str(payload.get("value") or "")
                if not draft_key:
                    self.send_error_json("缺少草稿 key。", status=400)
                    return
                project_db.save_form_draft(draft_key, draft_value)
                self.send_json({"ok": True})
                return
            if path == "/api/projects/bulk-delete":
                ids = payload.get("ids") or []
                if not isinstance(ids, list):
                    self.send_error_json("ids 必须是列表。", status=400)
                    return
                deleted = project_db.delete_projects(ids)
                self.send_json({"ok": True, "deleted": deleted})
                return
            if path == "/api/projects/snapshot":
                projects = payload.get("projects") or []
                if not isinstance(projects, list):
                    self.send_error_json("projects 必须是列表。", status=400)
                    return
                restored = project_db.replace_projects(projects)
                self.send_json({"ok": True, "projects": restored})
                return
            if path == "/api/outlook/open-email":
                result = open_email_in_outlook(payload)
                self.send_json(result)
                return
            if path == "/api/attachments/open":
                result = open_attachment_file(payload)
                self.send_json(result)
                return
            if path.startswith("/api/outlook/sync/") and path.endswith("/cancel"):
                parts = path.strip("/").split("/")
                job_id = parts[-2] if len(parts) >= 4 else ""
                with SYNC_LOCK:
                    job = SYNC_JOBS.get(job_id)
                    event = SYNC_CANCEL_EVENTS.get(job_id)
                    if not job or not event:
                        self.send_error_json("未找到可取消的同步任务。", status=404)
                        return
                    if job.get("status") in {"completed", "failed", "cancelled"}:
                        self.send_json({"ok": True, "job": job, "message": "任务已经结束。"})
                        return
                    event.set()
                    job["status"] = "cancelling"
                    job["updatedAt"] = now_text()
                job_log(job_id, "收到取消整合请求，正在停止扫描并回滚本次结果...")
                self.send_json({"ok": True, "message": "已请求取消整合。"})
                return
            if path == "/api/outlook/sync":
                job_id = uuid.uuid4().hex
                cancel_event = threading.Event()
                with SYNC_LOCK:
                    SYNC_CANCEL_EVENTS[job_id] = cancel_event
                    SYNC_JOBS[job_id] = {
                        "jobId": job_id,
                        "status": "queued",
                        "createdAt": now_text(),
                        "updatedAt": now_text(),
                        "logs": [{"time": now_text(), "message": "同步任务已创建。"}],
                        "result": None,
                        "error": "",
                    }
                thread = threading.Thread(target=run_sync_job, args=(job_id, payload), daemon=True)
                thread.start()
                self.send_json({"ok": True, "jobId": job_id})
                return

            # ===================================================
            # DOCUMENT CHECK POST API
            # ===================================================

            if path == "/api/document-check/cases":
                case = dcdb.create_review_case(payload.get("name", ""))
                self.send_json({"ok": True, "case": case})
                return

            if path.startswith("/api/document-check/cases/") and path.endswith("/documents"):
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" in content_type:
                    boundary = content_type.split("boundary=")[-1].strip()
                    file_content, filename = self._parse_multipart(boundary)
                    if not file_content:
                        self.send_error_json("未收到文件。", status=400)
                        return
                else:
                    file_b64 = payload.get("file_content", "")
                    filename = payload.get("original_filename", "upload.pdf")
                    import base64
                    try:
                        file_content = base64.b64decode(file_b64)
                    except Exception:
                        self.send_error_json("文件内容解码失败。", status=400)
                        return

                workspace = payload.get("workspace", "A")
                try:
                    doc = dcsvc.process_upload(case_id, file_content, filename, workspace)
                    self.send_json({"ok": True, "document": doc})
                except ValueError as ve:
                    self.send_error_json(str(ve), status=400)
                return

            if path.startswith("/api/document-check/cases/") and path.endswith("/run"):
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                dcsvc.start_review_job(case_id)
                self.send_json({"ok": True, "case_id": case_id, "status": "running"})
                return

            if path.startswith("/api/document-check/cases/") and path.endswith("/generate-bt09"):
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                try:
                    bt09 = dcsvc.generate_bt09_draft(case_id)
                    self.send_json({"ok": True, "bt09": bt09})
                except ValueError as ve:
                    self.send_error_json(str(ve), status=400)
                return

            if path.startswith("/api/document-check/cases/") and path.count("/") == 4:
                parts = [p for p in path.strip("/").split("/") if p]
                case_id = parts[3] if len(parts) >= 4 else ""
                if not case_id:
                    self.send_error_json("缺少 case ID。", status=400)
                    return
                if self.command == "DELETE":
                    dcdb.delete_review_case(case_id)
                    self.send_json({"ok": True})
                    return
                self.send_error_json("Method not supported。", status=405)
                return

            if path.startswith("/api/document-check/documents/") and path.count("/") == 4:
                parts = [p for p in path.strip("/").split("/") if p]
                doc_id = parts[3] if len(parts) >= 4 else ""
                if not doc_id:
                    self.send_error_json("缺少 document ID。", status=400)
                    return
                if self.command == "DELETE":
                    dcdb.delete_review_document(doc_id)
                    self.send_json({"ok": True})
                    return
                self.send_error_json("Method not supported。", status=405)
                return

            if path.startswith("/api/document-check/check-items/") and path.count("/") == 5:
                parts = [p for p in path.strip("/").split("/") if p]
                item_id = parts[4] if len(parts) >= 5 else ""
                if not item_id:
                    self.send_error_json("缺少 check item ID。", status=400)
                    return
                status_val = payload.get("status", "")
                summary = payload.get("summary", "")
                details = payload.get("details", "")
                item = dcdb.update_check_item_status(item_id, status_val, summary, details)
                self.send_json({"ok": True, "check_item": item})
                return

            if path.startswith("/api/document-check/fields/") and path.count("/") == 5:
                parts = [p for p in path.strip("/").split("/") if p]
                field_id = parts[4] if len(parts) >= 5 else ""
                if not field_id:
                    self.send_error_json("缺少 field ID。", status=400)
                    return
                new_value = payload.get("value", "")
                note = payload.get("note", "")
                source = payload.get("source", "manual")
                field = dcdb.apply_manual_override(field_id, new_value, note, source)
                self.send_json({"ok": True, "field": field})
                return

            self.send_error_json("未知 API。", status=404)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/projects/"):
                project_id = unquote(path.rsplit("/", 1)[-1])
                payload = self.read_json()
                payload["id"] = project_id
                payload["manualOverride"] = True
                project = project_db.upsert_project(payload)
                self.send_json({"ok": True, "project": project})
                return
            self.send_error_json("未知 API。", status=404)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/drafts/"):
                draft_key = unquote(path.rsplit("/", 1)[-1])
                project_db.delete_form_draft(draft_key)
                self.send_json({"ok": True})
                return
            if path.startswith("/api/projects/"):
                project_id = unquote(path.rsplit("/", 1)[-1])
                project_db.delete_project(project_id)
                self.send_json({"ok": True})
                return
            self.send_error_json("未知 API。", status=404)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def serve_static(self, path: str) -> None:
        full_path = safe_static_path(path)
        if not full_path:
            self.send_error_json("文件不存在。", status=404)
            return
        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        if full_path.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        elif full_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif full_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        with open(full_path, "rb") as fh:
            data = fh.read()
        self._send_headers(status=200, content_type=content_type)
        self.wfile.write(data)


def main() -> None:
    host = os.environ.get("PM_TRACKER_HOST", "127.0.0.1")
    port = int(os.environ.get("PM_TRACKER_PORT", "5050"))
    url = f"http://{host}:{port}/"
    httpd = ThreadingHTTPServer((host, port), PMRequestHandler)
    if os.environ.get("PM_TRACKER_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"PM Workplace running at {url}")
    print("Outlook will not sync until you click '同步 Outlook' in the UI.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()