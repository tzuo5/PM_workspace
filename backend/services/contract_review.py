# -*- coding: utf-8 -*-
"""Compatibility entry point for the contract review service.

The deterministic implementation remains in ``contract_review_engine``.  This
module adds a non-authoritative debug snapshot to each successful review so the
browser can export one AI-readable report without changing review decisions.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.contract_review_engine import *  # noqa: F401,F403
from services.contract_review_engine import __all__ as _ENGINE_ALL
from services.contract_review_engine import run_review as _engine_run_review
from services.contract_review_knowledge import get_contract_review_knowledge


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT_DIR = os.path.dirname(_BACKEND_DIR)
_PROMPT_RELATIVE_PATH = "backend/config/contract_checker_prompt"
_DEBUG_SOURCE_FILES = (
    "backend/services/contract_review.py",
    "backend/services/contract_review_engine.py",
    "backend/services/contract_review_knowledge.py",
    "backend/services/contract_llm_review.py",
    "backend/services/pdf_evidence.py",
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file_metadata(path: str, role: str) -> Dict[str, Any]:
    absolute = os.path.abspath(path)
    return {
        "role": role,
        "filename": os.path.basename(absolute),
        "size_bytes": os.path.getsize(absolute),
        "sha256": _sha256_file(absolute),
    }


def _role_for_path(path: str, file_roles: Optional[Dict[str, str]], index: int) -> str:
    absolute = os.path.abspath(path)
    for role, candidate in (file_roles or {}).items():
        if candidate and os.path.abspath(candidate) == absolute:
            return str(role)
    return f"unassigned_{index + 1}"


def _git_revision() -> str:
    """Read the checkout revision without invoking git or failing packaged runs."""
    git_dir = os.path.join(_ROOT_DIR, ".git")
    head_path = os.path.join(git_dir, "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as handle:
            head = handle.read().strip()
        if head.startswith("ref: "):
            ref_path = os.path.join(git_dir, head[5:].replace("/", os.sep))
            with open(ref_path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        return head
    except OSError:
        return ""


def _source_hashes() -> List[Dict[str, str]]:
    output: List[Dict[str, str]] = []
    for relative_path in _DEBUG_SOURCE_FILES:
        absolute = os.path.join(_ROOT_DIR, *relative_path.split("/"))
        if not os.path.isfile(absolute):
            continue
        output.append({"path": relative_path, "sha256": _sha256_file(absolute)})
    return output


def _build_debug_context(
    pdf_paths: List[str],
    file_roles: Optional[Dict[str, str]],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    knowledge = get_contract_review_knowledge()
    inputs = [
        _safe_file_metadata(path, _role_for_path(path, file_roles, index))
        for index, path in enumerate(pdf_paths)
        if path and os.path.isfile(path)
    ]
    review_items = result.get("review_items") if isinstance(result.get("review_items"), list) else []
    uncertain_evidence = 0
    for item in review_items:
        nodes = [item] + list(item.get("sub_items") or []) if isinstance(item, dict) else []
        for node in nodes:
            for evidence in node.get("evidence") or []:
                if evidence.get("location_status") != "exact":
                    uncertain_evidence += 1

    return {
        "schema_version": "pm-contract-debug-context/v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": "tzuo5/PM_workspace",
        "git_revision": _git_revision(),
        "prompt_path": _PROMPT_RELATIVE_PATH,
        "prompt_source_files": list(knowledge.source_files),
        "prompt_snapshot": knowledge.rule_context,
        "input_files": inputs,
        "source_hashes": _source_hashes(),
        "diagnostic_counters": {
            "review_item_count": len(review_items),
            "blocker_count": len(result.get("blockers") or []),
            "warning_count": len(result.get("non_blockers") or []),
            "uncertain_evidence_count": uncertain_evidence,
        },
        "note": (
            "This context is diagnostic only. The deterministic engine remains "
            "the authority for every review status and blocker decision."
        ),
    }


def run_review(
    pdf_paths: List[str],
    customer_db_path: str = None,
    template_path: str = None,
    file_roles: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run the deterministic review and attach a best-effort debug snapshot."""
    result = _engine_run_review(
        pdf_paths,
        customer_db_path=customer_db_path,
        template_path=template_path,
        file_roles=file_roles,
    )
    try:
        result["debug_context"] = _build_debug_context(pdf_paths, file_roles, result)
    except Exception as exc:  # Debug export must never break the contract review.
        result["debug_context"] = {
            "schema_version": "pm-contract-debug-context/v1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
    return result


__all__ = list(_ENGINE_ALL)
