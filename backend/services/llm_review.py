# -*- coding: utf-8 -*-
"""DeepSeek-powered double-check for Outlook contract workflow emails.

Supports any OpenAI-compatible API (DeepSeek, LM Studio, etc.).
Uses urllib from the standard library — no third-party dependencies required.

DeepSeek API: https://api.deepseek.com/v1/chat/completions
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DEFAULT_LLM_CONFIG_PATH = os.path.join(CONFIG_DIR, "llm_config.json")

ALLOWED_DECISIONS = {"confirmed", "review", "ignored"}
ALLOWED_STAGES = {
    "销售开启合同",
    "PM开启BT09",
    "PA回复SO/BT09",
    "iProcess审批",
    "Book订单申请",
    "工厂BT回复",
    "工厂反馈OA",
    None,
    "",
}

STAGE_ALIASES = {
    "销售合同已开启": "销售开启合同",
    "销售开启合同": "销售开启合同",
    "PM开启BT09": "PM开启BT09",
    "PM 开启BT09": "PM开启BT09",
    "PM开启合同BT09": "PM开启BT09",
    "PA回复SO/BT09": "PA回复SO/BT09",
    "PA回复SO号、BT09号": "PA回复SO/BT09",
    "iProcess审批": "iProcess审批",
    "iprocess审批": "iProcess审批",
    "Book订单申请": "Book订单申请",
    "book订单申请": "Book订单申请",
    "工厂BT回复": "工厂BT回复",
    "工厂反馈OA": "工厂反馈OA",
    "OA反馈": "工厂反馈OA",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def to_str(value: Any) -> str:
    return "" if value is None else str(value)


def truncate(value: Any, limit: int) -> str:
    text = to_str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def redact_key(key: Any) -> str:
    """Mask an API key for safe logging — only show first 6 + last 4 chars."""
    key = to_str(key)
    if len(key) <= 12:
        return key[:3] + "***" if len(key) > 6 else "***"
    return key[:6] + "..." + key[-4:]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_llm_config(path: str = DEFAULT_LLM_CONFIG_PATH) -> Dict[str, Any]:
    """Load LLM config from JSON file, with sensible defaults."""
    if not os.path.exists(path):
        return {
            "enabled": False,
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0,
            "max_tokens": 500,
            "timeout_seconds": 60,
            "max_retries": 3,
            "retry_delay_seconds": 2,
            "confirm_threshold": 0.75,
            "review_threshold": 0.45,
        }
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("enabled", True)
    cfg.setdefault("provider", "deepseek")
    cfg.setdefault("base_url", "https://api.deepseek.com/v1")
    cfg.setdefault("api_key", "")
    cfg.setdefault("model", "deepseek-chat")
    cfg.setdefault("temperature", 0)
    cfg.setdefault("max_tokens", 500)
    cfg.setdefault("timeout_seconds", 60)
    cfg.setdefault("max_retries", 3)
    cfg.setdefault("retry_delay_seconds", 2)
    cfg.setdefault("confirm_threshold", 0.75)
    cfg.setdefault("review_threshold", 0.45)
    return cfg


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def normalize_stage(stage: Any) -> str:
    text = to_str(stage).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return ""
    return STAGE_ALIASES.get(text, text)


def _strip_thinking_and_markdown(text: str) -> str:
    """DeepSeek may emit <think> tags or Markdown fences — strip them."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    return text


def extract_json_object(text: str) -> Dict[str, Any]:
    """Robust JSON extraction from model output."""
    cleaned = _strip_thinking_and_markdown(text)
    # Try direct parse first.
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # Try to find JSON object boundaries in the response.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Last-resort: try to fix common JSON issues.
    try:
        fixed = re.sub(r"([{,])(\w+)(\s*):", r'\1"\2"\3:', cleaned)
        return json.loads(fixed)
    except Exception:
        pass
    raise ValueError(f"AI 没有返回有效 JSON。原始输出: {truncate(text, 200)}")


def build_prompt(email: Dict[str, Any], rule_result: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build system + user messages for the DeepSeek API."""
    attachment_names = email.get("attachment_names") or []
    if not isinstance(attachment_names, list):
        attachment_names = []

    rule_decision = (
        "review" if rule_result.get("needs_review")
        else ("confirmed" if rule_result.get("is_valid") else "ignored")
    )
    rule_stage = rule_result.get("best_progress") or ""
    rule_score = rule_result.get("best_score")
    rule_matched = rule_result.get("matched") or []
    rule_reason = rule_result.get("review_reason") or ""

    payload = {
        "subject": truncate(email.get("subject"), 500),
        "current_body": truncate(email.get("current_body"), 1600),
        "attachment_names": [truncate(name, 160) for name in attachment_names[:12]],
        "attachment_count": len(attachment_names),
        "has_image": bool(email.get("has_image")),
        "received_time": to_str(email.get("received_time_text")),
        "contract_candidates": [c[0] for c in (email.get("contracts") or [])],
        "sender_folder": email.get("folder", ""),
        "has_business_numbers": _check_business_numbers(email),
        "rule_guess": {
            "decision": rule_decision,
            "stage": rule_stage,
            "score": rule_score,
            "matched": rule_matched[:5],
            "review_reason": rule_reason,
        },
        "missing_root_subject": bool(email.get("missing_root_subject")),
    }

    system = (
        "你是 ABB 中国项目订单流程邮件的智能审核器。"
        "你的职责是对邮件内容进行二次审核，判断其是否属于 ABB 合同流程，并确定具体阶段。"
        "你只输出一个 JSON 对象，不输出 Markdown、不输出解释、不输出思考过程。"
        "你的判断必须客观，信件内容为唯一依据，不得被发件人信息影响。"
    )
    user = f"""
标准合同流程阶段（只能选以下之一或其 null）：
1. 销售开启合同 — 销售发起合同流程的原始邮件。特征：主题含"请开启/开通/启动xxx合同流程"、"xxx合同流程开启"、"xxx合同OCR流程开启"；正文含发起流程、合同号（M/K4367-xxxx 或 CQxxxxxx）。
2. PM开启BT09 — PM请求创建 BT09 订单。特征：正文含"请创建/开启 BT09"、"请帮忙下单"、"建中间号"、"PO如附件"、"建工单"、"预付款已到"且提及BT09。
3. PA回复SO/BT09 — PA/SA 回复 SO 号和 BT/BTC 号。特征：正文同时出现 BTxxxxx 和 50xxxxxxx（SO号）；或回复含单独 BTC/BTY 号 + SO号；或跨公司调账SO回复。
4. iProcess审批 — iProcess 系统审批通知。特征：含 iProcess 链接、QueueID、AuthorizationHistory、display?pagetype；或"M4367-xxxx - 客户名 - 金额"格式的完成通知；或 RARO/SalesOrderApprovalRequest。
5. Book订单申请 — 提交 book in SAP 申请。特征：主题以"CHECK"开头，格式为"CHECK-M4367-xxxx-客户-金额"；正文含"book in SAP"、"请check并book"、"待Aimee book"。
6. 工厂BT回复 — 工厂回复下单 BT/BTC 号。特征：RFC 审批通过且含BT号；或下单成功确认。
7. 工厂反馈OA — 工厂最终反馈或 Order Acknowledgement。特征：主题含"Your ABB Order"、"Order Acknowledgement"、"OA"；或正文含确认收货/反馈。

分类标准 decision：
- confirmed：邮件明确是上述某一阶段，证据充分。confidence ≥ 0.80。
- review：可能相关但证据不足、仅有 RE/FW 缺少原始邮件、规则与语义冲突、阶段模棱两可。confidence 0.45–0.79。
- ignored：不是标准合同流程邮件（普通报价、会议纪要、内部通知、无流程动作的沟通）。confidence < 0.45。

硬性规则：
- 必须基于 current_body（最新正文）判断，历史引用区内容只能作为辅助上下文，不能作为主要判断依据。
- 如果 missing_root_subject=true，说明本次扫描只扫到 RE/FW 后续邮件，缺少原始销售开启邮件。这种情况下即使内容像流程邮件，decision 也要设为 review，不能 confirmed。在 reason 中注明这一限制。
- 如果邮件没有合同号（M/K4367-xxx、CQxxxxx、SO号、BT号），且没有明确流程动作，应 ignored。
- 如果邮件有明确的 BT/SO/CQ 号但缺 M/K4367 合同号，可能是流程后段邮件，设为 review 并标注"缺合同号"。
- 附件名中含"合同"、"SO"、"BT"、"订单"、"PO"、"Quotation"时，可提升相关性判断。
- 不要从发件人姓名、邮箱地址、收件人信息推断阶段。
- 如果规则已给出具体分数较高的阶段（score ≥ 100），且你的判断与规则一致且证据充分，直接 confirmed。
- 如果规则判定为 review（分数 50–99），你需要独立重新判断，不可盲从规则。

返回 JSON 格式（严格遵守）：
{{
  "decision": "confirmed|review|ignored",
  "stage": "销售开启合同|PM开启BT09|PA回复SO/BT09|iProcess审批|Book订单申请|工厂BT回复|工厂反馈OA|null",
  "contract_number": "M4367-3380 或空字符串",
  "company_name": "完整公司名或空字符串",
  "confidence": 0.0,
  "reason": "一句话中文原因，说明判断依据",
  "evidence": ["原文关键片段1", "原文关键片段2"]
}}

邮件数据：
{json.dumps(payload, ensure_ascii=False)}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _check_business_numbers(email: Dict[str, Any]) -> Dict[str, bool]:
    """Summarize which business identifiers are present in the email."""
    subject = to_str(email.get("subject", ""))
    body = to_str(email.get("current_body", "")) + "\n" + to_str(email.get("body", ""))
    combined = subject + "\n" + body
    return {
        "has_mk4367": bool(re.search(r"[MK]4367-\d{4}", combined, re.I)),
        "has_cq": bool(re.search(r"CQ\d{5,}", combined, re.I)),
        "has_so": bool(re.search(r"50\d{5,}", combined)),
        "has_bt": bool(re.search(r"BT[A-Z]?\d{4,}", combined, re.I)),
        "has_ocr": bool(re.search(r"OCR\d*", combined, re.I)),
    }


# ---------------------------------------------------------------------------
# API communication
# ---------------------------------------------------------------------------

def _is_retryable_http_error(code: int) -> bool:
    """Return True for HTTP errors that should trigger a retry."""
    return code in (429, 500, 502, 503, 504)


def _build_request(url: str, data_bytes: bytes, headers: Dict[str, str], timeout: int) -> urllib.request.Request:
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
    return req


def call_llm_chat(config: Dict[str, Any], messages: List[Dict[str, str]]) -> str:
    """Send a chat completion request with retry + rate-limit handling."""
    base_url = to_str(config.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    url = base_url + "/chat/completions"
    max_retries = int(config.get("max_retries", 3))
    retry_delay = float(config.get("retry_delay_seconds", 2))
    timeout = int(config.get("timeout_seconds", 60))
    api_key = to_str(config.get("api_key", "")).strip()

    data = {
        "model": config.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": float(config.get("temperature", 0)),
        "max_tokens": int(config.get("max_tokens", 700)),
        "stream": False,
    }
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")

    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload["choices"][0]["message"]["content"]

        except urllib.error.HTTPError as exc:
            status = exc.code
            last_error = exc
            if status == 401:
                raise RuntimeError(
                    f"DeepSeek API 认证失败（401）。请检查 API Key 是否正确。"
                    f"当前 Key: {redact_key(api_key)}"
                ) from exc
            if status == 402:
                raise RuntimeError(
                    "DeepSeek 账户余额不足（402）。请充值后重试。"
                ) from exc
            if status == 429 and attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                time.sleep(wait)
                continue
            if _is_retryable_http_error(status) and attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"DeepSeek API 返回 HTTP {status}（attempt {attempt + 1}/{max_retries + 1}）"
            ) from exc

        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
            if attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"无法连接 DeepSeek API（{base_url}）：{exc}"
            ) from exc

        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                time.sleep(wait)
                continue
            raise RuntimeError(f"DeepSeek API 调用失败：{exc}") from exc

    raise RuntimeError(
        f"DeepSeek API 在 {max_retries + 1} 次尝试后仍然失败。最后错误: {last_error}"
    )


# ---------------------------------------------------------------------------
# Response normalization & validation
# ---------------------------------------------------------------------------

def normalize_llm_response(data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the LLM response."""
    decision = to_str(data.get("decision")).strip().lower()
    if decision not in ALLOWED_DECISIONS:
        decision = "review"

    stage = normalize_stage(data.get("stage"))
    if stage not in ALLOWED_STAGES:
        stage = ""
        if decision == "confirmed":
            decision = "review"

    try:
        confidence = float(data.get("confidence", 0))
    except Exception:
        confidence = 0.0
    if confidence > 1:
        confidence = confidence / 100.0
    confidence = max(0.0, min(1.0, confidence))

    # Enforce confidence thresholds: confirmed must be ≥ confirm_threshold.
    confirm_threshold = float(config.get("confirm_threshold", 0.75))
    review_threshold = float(config.get("review_threshold", 0.45))

    if decision == "confirmed" and confidence < confirm_threshold:
        decision = "review"
    if decision == "ignored" and confidence > review_threshold:
        # Model says ignored but with high confidence — accept it.
        pass

    evidence = data.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [to_str(evidence)]
    evidence = [truncate(item, 180) for item in evidence if to_str(item).strip()][:3]

    reason = truncate(data.get("reason"), 300)

    return {
        "decision": decision,
        "stage": stage,
        "contract_number": to_str(data.get("contract_number")).strip().upper(),
        "company_name": to_str(data.get("company_name")).strip(),
        "confidence": confidence,
        "reason": reason,
        "evidence": evidence,
        "raw": data,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review_email(
    email: Dict[str, Any],
    rule_result: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Second-pass LLM review of a single email.

    Args:
        email: Parsed email dict from outlook_sync.
        rule_result: First-pass rule classification result.
        config: LLM config dict (loaded from llm_config.json if None).

    Returns:
        Normalized review dict with decision, stage, confidence, etc.
    """
    cfg = config or load_llm_config()

    api_key = to_str(cfg.get("api_key", "")).strip()
    if not api_key:
        raise RuntimeError(
            "DeepSeek API Key 未配置。请在 backend/config/llm_config.json 中设置 api_key。"
        )

    messages = build_prompt(email, rule_result)
    content = call_llm_chat(cfg, messages)
    parsed = extract_json_object(content)
    return normalize_llm_response(parsed, cfg)


def health_check(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Quick connectivity check against the configured API endpoint.

    Uses POST to /chat/completions with a minimal "ping" message instead of
    GET /models, because DeepSeek's /models endpoint is not publicly available.
    """
    cfg = config or load_llm_config()
    base_url = to_str(cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    url = base_url + "/chat/completions"
    api_key = to_str(cfg.get("api_key", "")).strip()
    timeout = min(int(cfg.get("timeout_seconds", 90)), 15)

    if not api_key:
        return {"ok": False, "error": "API Key 未配置。"}

    # Minimal ping request.
    ping_data = json.dumps({
        "model": cfg.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(url, data=ping_data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        # A valid response contains choices array.
        if "choices" in payload:
            return {
                "ok": True,
                "model": cfg.get("model"),
                "provider": cfg.get("provider"),
            }
        return {"ok": False, "error": f"非期望响应: {truncate(json.dumps(payload), 200)}"}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": f"认证失败（401）。请检查 API Key: {redact_key(api_key)}"}
        if exc.code == 402:
            return {"ok": False, "error": "账户余额不足（402），请充值。"}
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}