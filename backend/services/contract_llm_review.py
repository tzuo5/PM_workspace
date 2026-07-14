# -*- coding: utf-8 -*-
"""DeepSeek-powered LLM review for contract comparison.

Takes the extracted contract/CQP/TA data and consistency check results,
sends them to DeepSeek for a comprehensive AI review — identifying risks,
inconsistencies, and providing natural-language recommendations.

Reuses the API communication layer from llm_review.py.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from services.llm_review import (
    call_llm_chat,
    extract_json_object,
    load_llm_config,
    redact_key,
    to_str,
    truncate,
)


def build_contract_review_prompt(
    contract_data: Dict[str, Any],
    cqp_data: Dict[str, Any],
    ta_data: Dict[str, Any],
    incoterm_result: Dict[str, Any],
    consistency_results: List[Dict[str, Any]],
    warranty_result: Dict[str, Any],
    config_result: Dict[str, Any],
    financial_result: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Build system + user messages for the contract comparison LLM review."""

    # Build a structured payload with all extracted data
    payload = {
        "contract": _summarize_contract(contract_data),
        "cqp": _summarize_cqp(cqp_data),
        "ta": _summarize_ta(ta_data),
        "incoterm": {
            "conclusion": incoterm_result.get("conclusion", ""),
            "contract_evidence": incoterm_result.get("contract_evidence", ""),
            "cqp_evidence": incoterm_result.get("cqp_evidence", ""),
            "consistent": incoterm_result.get("consistent", True),
        },
        "consistency_checks": [
            {
                "name": c.get("check_name", ""),
                "status": c.get("status", ""),
                "detail": c.get("detail", ""),
            }
            for c in consistency_results
        ],
        "warranty": {
            "consistent": warranty_result.get("consistent", True),
            "detail": warranty_result.get("detail", ""),
            "contract_warranty": warranty_result.get("contract_warranty", []),
            "cqp_codes": warranty_result.get("cqp_warranty_codes", []),
        },
        "configuration": {
            "overall_consistent": config_result.get("overall_consistent", True),
            "cqp_only_codes": _get_config_codes(config_result, "cqp_only_codes"),
            "ta_only_codes": _get_config_codes(config_result, "ta_only_codes"),
            "desc_mismatches": _get_config_desc_mismatches(config_result),
        },
        "financial": {
            "vat": financial_result.get("vat_check", {}),
            "untaxed": financial_result.get("untaxed_check", {}),
            "tax_included": financial_result.get("tax_included_check", {}),
        },
    }

    system = (
        "你是 ABB 中国机器人销售合同审核专家。"
        "你的职责是对合同（Contract）、报价单（CQP）和技术协议（TA）的对比结果进行综合评审。"
        "请基于提供的结构化提取数据，给出专业的审核意见。"
        "你只输出一个 JSON 对象，不输出 Markdown、不输出解释、不输出思考过程。"
    )

    user = f"""
请对以下合同对比数据进行综合AI审核：

## 审核要求

1. **整体评估**：根据所有数据给出 overall_assessment（Pass / Pass with notes / Blocked）
2. **关键风险**：列出所有需要关注的 blocker 级别问题，每条包含 risk 和 suggestion
3. **非阻塞问题**：列出所有 non-blocker 级别的问题，每条包含 issue 和 note
4. **数据完整性**：检查是否存在缺失的关键字段，给出 completeness_notes
5. **推荐操作**：给出下一步建议的 next_steps（列表）
6. **审核总结**：用中文写一段 2-4 句话的 summary，概括整体情况

## 判断标准

- Blocked：存在合同号不一致、贸易术语冲突、型号不匹配、数量不一致、质保冲突等严重问题
- Pass with notes：存在轻微差异（如翻译差异、舍入误差、非关键字段缺失），但核心条款一致
- Pass：所有关键字段一致，无任何差异

## 提取数据

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

## 返回 JSON 格式（严格遵守）

{{
  "overall_assessment": "Pass|Pass with notes|Blocked",
  "key_risks": [
    {{"risk": "风险描述", "severity": "high|medium|low", "suggestion": "建议措施"}}
  ],
  "non_blocker_issues": [
    {{"issue": "问题描述", "note": "备注说明"}}
  ],
  "completeness_notes": "数据完整性评估（中文）",
  "next_steps": ["建议1", "建议2"],
  "summary": "综合审核总结（2-4句中文）",
  "confidence": 0.0
}}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _summarize_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a clean summary of contract extracted fields."""
    return {
        "contract_number": data.get("contract_number", ""),
        "seller_name": data.get("seller_name", ""),
        "buyer_name": data.get("buyer_name", ""),
        "buyer_address": truncate(data.get("buyer_address", ""), 200),
        "end_customer_name": data.get("end_customer_name", ""),
        "end_customer_address": truncate(data.get("end_customer_address", ""), 200),
        "robot_models": data.get("robot_models", []),
        "total_qty": data.get("total_qty", 0),
        "incoterm_selection": data.get("incoterm_selection", ""),
        "delivery_location": data.get("delivery_location", ""),
        "delivery_time": data.get("delivery_time", []),
        "payment_terms_annex2": truncate(data.get("payment_terms_annex2", ""), 500),
        "warranty_clause_5_2": data.get("warranty_clause_5_2", {}),
        "vat_rate": data.get("vat_rate", 0),
        "untaxed_amount": data.get("untaxed_amount", 0),
        "tax_included_amount": data.get("tax_included_amount", 0),
        "sales_person": data.get("sales_person", ""),
        "pm": data.get("pm", ""),
    }


def _summarize_cqp(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a clean summary of CQP extracted fields."""
    return {
        "cqp_number": data.get("cqp_number", ""),
        "customer_name": data.get("customer_name", ""),
        "customer_address": truncate(data.get("customer_address", ""), 200),
        "end_user": data.get("end_user", ""),
        "delivery_term": data.get("delivery_term", ""),
        "delivery_time": data.get("delivery_time", ""),
        "payment_terms": truncate(data.get("payment_terms", ""), 300),
        "warranty_terms": data.get("warranty_terms", ""),
        "robot_models": data.get("robot_models", []),
        "untaxed_total": data.get("untaxed_total", 0),
        "vat_rate": data.get("vat_rate", 0),
        "tax_included_total": data.get("tax_included_total", 0),
        "warranty_codes": data.get("warranty_codes", []),
    }


def _summarize_ta(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a clean summary of TA extracted fields."""
    return {
        "robot_models": data.get("robot_models", []),
        "warranty_codes": data.get("warranty_codes", []),
    }


def _get_config_codes(config_result: Dict[str, Any], key: str) -> List[str]:
    """Extract config code lists from config result."""
    models = config_result.get("models_compared", [])
    if not models:
        return []
    return models[0].get(key, []) if models else []


def _get_config_desc_mismatches(config_result: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract description mismatches from config result."""
    models = config_result.get("models_compared", [])
    if not models:
        return []
    mismatches = models[0].get("description_mismatches", [])
    return [
        {"code": m.get("code", ""), "cqp_desc": m.get("cqp_desc", ""), "ta_desc": m.get("ta_desc", "")}
        for m in mismatches
    ]


def normalize_contract_llm_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the LLM contract review response."""
    assessment = to_str(data.get("overall_assessment", "")).strip()
    allowed = {"Pass", "Pass with notes", "Blocked"}
    if assessment not in allowed:
        assessment = "Pass with notes"

    key_risks = data.get("key_risks") or []
    if not isinstance(key_risks, list):
        key_risks = []
    key_risks = [
        {
            "risk": to_str(r.get("risk", "")),
            "severity": to_str(r.get("severity", "medium")),
            "suggestion": to_str(r.get("suggestion", "")),
        }
        for r in key_risks[:10]
    ]

    non_blocker_issues = data.get("non_blocker_issues") or []
    if not isinstance(non_blocker_issues, list):
        non_blocker_issues = []
    non_blocker_issues = [
        {
            "issue": to_str(i.get("issue", "")),
            "note": to_str(i.get("note", "")),
        }
        for i in non_blocker_issues[:10]
    ]

    next_steps = data.get("next_steps") or []
    if not isinstance(next_steps, list):
        next_steps = []
    next_steps = [to_str(s) for s in next_steps[:8] if to_str(s).strip()]

    try:
        confidence = float(data.get("confidence", 0))
    except (ValueError, TypeError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "overall_assessment": assessment,
        "key_risks": key_risks,
        "non_blocker_issues": non_blocker_issues,
        "completeness_notes": to_str(data.get("completeness_notes", "")),
        "next_steps": next_steps,
        "summary": truncate(data.get("summary", ""), 600),
        "confidence": confidence,
    }


def run_llm_contract_review(
    contract_data: Dict[str, Any],
    cqp_data: Dict[str, Any],
    ta_data: Dict[str, Any],
    incoterm_result: Dict[str, Any],
    consistency_results: List[Dict[str, Any]],
    warranty_result: Dict[str, Any],
    config_result: Dict[str, Any],
    financial_result: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run DeepSeek LLM review on contract comparison results.

    Returns a dict with overall_assessment, key_risks, non_blocker_issues,
    completeness_notes, next_steps, summary, and confidence.
    Returns an error dict if the LLM call fails.
    """
    cfg = config or load_llm_config()

    api_key = to_str(cfg.get("api_key", "")).strip()
    if not api_key:
        return {
            "error": "DeepSeek API Key 未配置。请在 backend/config/llm_config.json 中设置 api_key。",
            "overall_assessment": "Unknown",
            "summary": "（AI 审核未执行：API Key 未配置）",
        }

    try:
        messages = build_contract_review_prompt(
            contract_data, cqp_data, ta_data,
            incoterm_result, consistency_results,
            warranty_result, config_result, financial_result,
        )
        content = call_llm_chat(cfg, messages)
        parsed = extract_json_object(content)
        return normalize_contract_llm_response(parsed)
    except Exception as exc:
        return {
            "error": str(exc),
            "overall_assessment": "Unknown",
            "summary": f"（AI 审核失败：{truncate(str(exc), 200)}）",
        }