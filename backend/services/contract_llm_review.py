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
    """Build system + user messages for a narrative review of deterministic checks."""
    payload = {
        "contract": _summarize_contract(contract_data),
        "cqp": _summarize_cqp(cqp_data),
        "ta": _summarize_ta(ta_data),
        "incoterm": dict(incoterm_result),
        "consistency_checks": [
            {
                "name": check.get("check_name", ""),
                "status": check.get("status", ""),
                "detail": check.get("detail", ""),
                "is_blocker": bool(check.get("is_blocker")),
            }
            for check in consistency_results
        ],
        "warranty": dict(warranty_result),
        "configuration": dict(config_result),
        "financial": {
            "vat": financial_result.get("vat_check", {}),
            "untaxed": financial_result.get("untaxed_check", {}),
            "tax_included": financial_result.get("tax_included_check", {}),
        },
    }

    system = (
        "你是ABB中国机器人销售合同审核助手。规则引擎已经完成证据提取与BLOCKER分级，"
        "你只能解释和汇总这些结果，不得推翻规则引擎状态，不得补造文件中不存在的字段。"
        "审核优先级为：合同作为法律依据；CQP用于商业报价验证；TA用于技术配置闭环。"
        "只输出一个JSON对象，不输出Markdown、解释或思考过程。"
    )

    user = f"""
请根据以下规则检查结果生成中文审核摘要。

必须遵守：
1. 只要 consistency_checks 中存在 is_blocker=true，overall_assessment 必须为 Blocked。
2. 没有BLOCKER但存在 WARNING/UNDETERMINED 时，必须为 Pass with notes。
3. 合同交期与CQP不同通常是非阻塞说明，BT09以合同为准；不得仅因此判Blocked。
4. 合同与CQP金额差异小于人民币1元属于舍入说明，不得仅因此判Blocked。
5. TA可以是独立文件，也可以嵌在合同附件中；只有两处都不存在时才判缺失。
6. Incoterm勾选OCR不清不等于阻塞；若合同交付地点与CQP证据可合理推定，应保留备注。
7. 付款条件必须引用合同附件二原文，不能简写为比例组合。
8. 中英文名称或代码/描述能够映射到同一配置时，属于表述差异，不得判技术不一致。

提取与检查数据：
{json.dumps(payload, ensure_ascii=False, indent=2)}

严格返回：
{{
  "overall_assessment": "Pass|Pass with notes|Blocked",
  "key_risks": [{{"risk": "风险描述", "severity": "high|medium|low", "suggestion": "建议措施"}}],
  "non_blocker_issues": [{{"issue": "问题描述", "note": "备注说明"}}],
  "completeness_notes": "数据完整性评估",
  "next_steps": ["下一步"],
  "summary": "2-4句中文总结",
  "confidence": 0.0
}}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _summarize_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "contract_number": data.get("contract_number", ""),
        "cqp_reference": data.get("cqp_reference", ""),
        "seller_name": data.get("seller_name", ""),
        "buyer_name": data.get("buyer_name", ""),
        "buyer_address": truncate(data.get("buyer_address", ""), 300),
        "end_customer_name": data.get("end_customer_name", ""),
        "end_customer_address": truncate(data.get("end_customer_address", ""), 300),
        "delivery_location": data.get("delivery_location", ""),
        "products": data.get("products", []),
        "total_qty": data.get("total_qty", 0),
        "incoterm_detection": data.get("incoterm_detection", {}),
        "delivery_schedule": data.get("delivery_schedule", []),
        "delivery_trigger": data.get("delivery_trigger", ""),
        "payment_terms_annex2": truncate(data.get("payment_terms", {}).get("raw", ""), 1500),
        "warranty_clause_5_2": data.get("warranty", {}),
        "vat_rate": data.get("vat_rate", 0),
        "untaxed_amount": data.get("untaxed_amount", 0),
        "tax_included_amount": data.get("tax_included_amount", 0),
        "sales_person": data.get("sales_person", ""),
        "pm": data.get("pm", ""),
    }


def _summarize_cqp(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cqp_number": data.get("cqp_number", ""),
        "version": data.get("version", ""),
        "customer_name": data.get("customer_name", ""),
        "customer_address": truncate(data.get("customer_address", ""), 300),
        "end_user": data.get("end_user", ""),
        "delivery_term": data.get("delivery_term", ""),
        "delivery_time": data.get("delivery_time", ""),
        "payment_terms": truncate(data.get("payment_terms", {}).get("raw", ""), 1000),
        "warranty_terms": data.get("warranty_terms", ""),
        "products": data.get("products", []),
        "total_qty": data.get("total_qty", 0),
        "untaxed_total": data.get("untaxed_total", 0),
        "vat_rate": data.get("vat_rate", 0),
        "tax_included_total": data.get("tax_included_total", 0),
        "warranty_codes_by_model": data.get("warranty_codes_by_model", {}),
        "configurations": data.get("configurations", []),
    }


def _summarize_ta(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "contract_number": data.get("contract_number", ""),
        "buyer_name": data.get("buyer_name", ""),
        "seller_name": data.get("seller_name", ""),
        "products": data.get("products", []),
        "total_qty": data.get("total_qty", 0),
        "warranty_codes_by_model": data.get("warranty_codes_by_model", {}),
        "configurations": data.get("configurations", []),
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
        normalized = normalize_contract_llm_response(parsed)

        # The LLM is narrative-only. Enforce the deterministic rule-engine
        # conclusion in code rather than trusting prompt compliance.
        has_blocker = any(bool(check.get("is_blocker")) for check in consistency_results)
        has_notes = any(
            str(check.get("status", "")).upper() in {"WARNING", "UNDETERMINED", "MISMATCH"}
            for check in consistency_results
        )
        normalized["overall_assessment"] = (
            "Blocked" if has_blocker else ("Pass with notes" if has_notes else "Pass")
        )
        return normalized
    except Exception as exc:
        return {
            "error": str(exc),
            "overall_assessment": "Unknown",
            "summary": f"（AI 审核失败：{truncate(str(exc), 200)}）",
        }