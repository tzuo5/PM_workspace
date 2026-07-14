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
import re
import shutil
import subprocess
import sys
import tempfile
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
from services.contract_review import run_review  # noqa: E402

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
    """Run one Outlook sync job in a background thread.

    Outlook automation uses Windows COM. COM initialization is thread-local, so
    the worker thread must call pythoncom.CoInitialize() before touching Outlook.
    Without this, pywin32 raises: "CoInitialize has not been called."
    """
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
            # sync_outlook/connect_outlook will raise the user-facing pywin32 message.
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
    """Open the source Outlook MailItem in the classic Outlook client.

    The front end sends either latestEmailEntryId/latestEmailStoreId directly or
    a project id/contract. For older synced rows that do not yet have the
    latestEmailEntryId columns, we fall back to the latest stored email record
    for that contract.
    """
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
            # Some Outlook profiles resolve the item only without StoreID.
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
    """Open one saved Outlook attachment with the local OS default app.

    The front end passes a database attachment id, not an arbitrary filesystem
    path.  The backend then checks the row belongs to the current project and
    that the file still exists before opening it.
    """
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
        # Keep terminal output focused; uncomment for request debugging.
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
            if path == "/api/contract-review":
                result = handle_contract_review(self)
                self.send_json(result)
                return
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


def handle_contract_review(handler: PMRequestHandler) -> Dict[str, Any]:
    """Handle contract review API: accept PDF file uploads via multipart form data."""
    content_type = handler.headers.get("Content-Type", "")
    temp_dir = None
    try:
        if "multipart/form-data" in content_type:
            # Parse multipart form data with named fields
            pdf_paths, file_roles, temp_dir = _parse_multipart_upload(handler)
        else:
            # Try JSON with file paths
            payload = handler.read_json()
            pdf_paths = payload.get("pdf_paths") or payload.get("paths") or []
            file_roles = payload.get("file_roles") or None
            if isinstance(pdf_paths, str):
                pdf_paths = [pdf_paths]

        if not pdf_paths:
            raise ValueError("请上传至少一个PDF文件（Contract 和 CQP）")

        # Validate paths exist
        for p in pdf_paths:
            if not os.path.exists(p):
                raise FileNotFoundError(f"文件不存在: {p}")

        # Run review with explicit file roles
        result = run_review(pdf_paths, file_roles=file_roles)
        result["ok"] = True
        return result
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB limit
PDF_SIGNATURE = b"%PDF-"


def _validate_pdf_data(data: bytes, filename: str) -> bytes:
    """Validate that uploaded data is a PDF file."""
    if len(data) > MAX_FILE_SIZE:
        raise ValueError(f"文件 {filename} 超过大小限制（50 MB）")
    if not data.startswith(PDF_SIGNATURE):
        raise ValueError(f"文件 {filename} 不是有效的 PDF 文件（缺少 PDF 文件头）")
    return data


def _parse_multipart_upload(handler: PMRequestHandler) -> Tuple[List[str], Dict[str, str], str]:
    """Parse multipart form data with named fields.

    Returns: (all_paths, file_roles, temp_dir)
      - all_paths: list of all saved file paths
      - file_roles: dict mapping role names (contract/cqp/ta) to file paths
      - temp_dir: temp directory for cleanup
    """
    content_type = handler.headers.get("Content-Type", "")
    # Extract boundary
    boundary_match = re.search(r"boundary=(.+)", content_type)
    if not boundary_match:
        raise ValueError("无法解析 multipart boundary")

    boundary = boundary_match.group(1).strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]

    # Read body
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        raise ValueError("请求体为空")

    raw = handler.rfile.read(length)
    boundary_bytes = ("--" + boundary).encode("utf-8")

    # Create temp directory
    temp_dir = tempfile.mkdtemp(prefix="cr_upload_")
    saved_paths: List[str] = []
    file_roles: Dict[str, str] = {}

    # Known field names for file roles
    ROLE_FIELDS = {"contract", "cqp", "ta"}

    # Split by boundary
    parts = raw.split(boundary_bytes)
    for part in parts:
        if b"Content-Disposition" not in part:
            continue

        # Extract field name
        name_match = re.search(rb'name="(.+?)"', part)
        field_name = name_match.group(1).decode("utf-8", errors="replace") if name_match else ""

        # Extract filename
        filename_match = re.search(rb'filename="(.+?)"', part)
        if not filename_match:
            continue

        filename = filename_match.group(1).decode("utf-8", errors="replace")
        filename = os.path.basename(filename)  # safety

        # Find end of headers
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            # Try single newline separation
            header_end = part.find(b"\n\n")
            if header_end < 0:
                continue

        file_data = part[header_end + 4:]
        if header_end > 0 and part[header_end:header_end + 4] != b"\r\n\r\n":
            file_data = part[header_end + 2:]

        # Trim trailing \r\n and boundary markers
        file_data = file_data.rstrip(b"\r\n")
        if file_data.endswith(b"--"):
            file_data = file_data[:-2].rstrip(b"\r\n")

        if not file_data:
            continue

        # Validate PDF
        file_data = _validate_pdf_data(file_data, filename)

        filepath = os.path.join(temp_dir, filename)
        with open(filepath, "wb") as f:
            f.write(file_data)
        saved_paths.append(filepath)

        # Assign to role if field name matches
        if field_name in ROLE_FIELDS:
            if field_name in file_roles:
                # Another file already uploaded for this role — use the latest one
                pass
            file_roles[field_name] = filepath

    return saved_paths, file_roles, temp_dir


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
