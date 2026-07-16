# -*- coding: utf-8 -*-
"""Readable terminal logging for contract-review LLM calls.

The contract checker runs locally, so showing the complete prompt and model
response in the same terminal makes every AI-assisted review auditable without
changing the deterministic review result.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List

_PRINT_LOCK = threading.Lock()
_SEPARATOR = "=" * 96
_SUB_SEPARATOR = "-" * 96


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _print_request(request_id: str, config: Dict[str, Any], messages: List[Dict[str, str]]) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    provider = _text(config.get("provider") or "openai-compatible")
    model = _text(config.get("model") or "unknown")
    base_url = _text(config.get("base_url") or "")
    with _PRINT_LOCK:
        print("\n" + _SEPARATOR, flush=True)
        print(f"LLM CONSOLE | REQUEST {request_id} | {timestamp}", flush=True)
        print(f"Provider: {provider} | Model: {model} | Base URL: {base_url}", flush=True)
        for index, message in enumerate(messages, start=1):
            role = _text(message.get("role") or "message").upper()
            print(_SUB_SEPARATOR, flush=True)
            print(f"PROMPT {index} / {role}", flush=True)
            print(_text(message.get("content")), flush=True)
        print(_SEPARATOR, flush=True)


def _print_response(request_id: str, response: str, elapsed_seconds: float) -> None:
    with _PRINT_LOCK:
        print("\n" + _SEPARATOR, flush=True)
        print(f"LLM CONSOLE | RESPONSE {request_id} | {elapsed_seconds:.2f}s", flush=True)
        print(_SUB_SEPARATOR, flush=True)
        print(_text(response), flush=True)
        print(_SEPARATOR + "\n", flush=True)


def _print_error(request_id: str, error: BaseException, elapsed_seconds: float) -> None:
    with _PRINT_LOCK:
        print("\n" + _SEPARATOR, flush=True)
        print(f"LLM CONSOLE | ERROR {request_id} | {elapsed_seconds:.2f}s", flush=True)
        print(_SUB_SEPARATOR, flush=True)
        print(f"{type(error).__name__}: {error}", flush=True)
        print(_SEPARATOR + "\n", flush=True)


def logged_llm_call(
    call: Callable[..., Any],
    config: Dict[str, Any],
    messages: List[Dict[str, str]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run one LLM call, forwarding optional API kwargs and logging content."""
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    _print_request(request_id, config, messages)
    try:
        response = call(config, messages, *args, **kwargs)
    except BaseException as exc:
        _print_error(request_id, exc, time.perf_counter() - started)
        raise
    printable = response.get("content", "") if isinstance(response, dict) else response
    _print_response(request_id, _text(printable), time.perf_counter() - started)
    return response



def install_contract_review_console() -> None:
    """Wrap the contract-review module's LLM call once per interpreter."""
    from services import contract_llm_review

    current = contract_llm_review.call_llm_chat
    if getattr(current, "_pm_llm_console_wrapped", False):
        return

    def wrapped(
        config: Dict[str, Any],
        messages: List[Dict[str, str]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return logged_llm_call(current, config, messages, *args, **kwargs)

    wrapped._pm_llm_console_wrapped = True  # type: ignore[attr-defined]
    wrapped._pm_llm_console_original = current  # type: ignore[attr-defined]
    contract_llm_review.call_llm_chat = wrapped

