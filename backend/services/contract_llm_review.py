# -*- coding: utf-8 -*-
"""Narrative LLM review for deterministic contract-comparison results.

The rule engine owns every status and blocker decision.  The LLM receives the
maintained prompt knowledge only to explain findings and propose next steps; it
cannot override deterministic results or invent missing document facts.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from services.contract_review_knowledge import get_contract_review_knowledge
from services.llm_review import (
    call_llm_chat,
    extract_json_object,
    load_llm_config,
    to_str,
    truncate,
)


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
        "ship_to_name": data.get("ship_to_name", ""),
        "ship_to_address": data.get("ship_to_address", ""),
        "products": data.get("products", []),
        "total_qty": data.get("total_qty", 0),
        "incoterm_detection": data.get("incoterm_detection", {}),
        "delivery_schedule": data.get("delivery_schedule", []),
        "delivery_trigger": data.get("delivery_trigger", ""),
        "payment_terms_annex2": truncate(data.get("payment_terms", {}).get("raw", ""), 1800),
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
        "payment_terms": truncate(data.get("payment_terms", {}).get("raw", ""), 1200),
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
    """Build a source-grounded narrative prompt from deterministic findings."""
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
    knowledge = get_contract_review_knowledge()
    # Include the maintained agent files as secondary explanatory guidance.  The
    # deterministic check payload remains the only authority for status fields.
    rule_context = knowledge.rule_context[:40000]
    system = (
        "你是ABB中国机器人销售合同审核助手。Python规则引擎已完成证据提取和BLOCKER分级。"
        "你只能解释、归纳和提出下一步，不得推翻任何状态，不得补造文件中不存在的字段。"
        "法律事实优先级：合同 > CQP商业验证 > TA技术闭环。只输出一个JSON对象。"
    )
    user = f"""
请根据确定性检查结果生成中文审核摘要。

强制约束：
1. consistency_checks存在is_blocker=true时，overall_assessment必须为Blocked。
2. 无BLOCKER但存在WARNING/UNDETERMINED/MISMATCH时，必须为Pass with notes。
3. 合同与CQP交期不同通常是非阻塞说明，BT09以合同为准。
4. 金额差异小于人民币1元属于舍入说明。
5. TA可独立存在或嵌入合同；两处均不存在才算缺失。
6. Incoterm勾选OCR不清不自动阻断；可用无冲突fallback证据推定。
7. 付款条件必须保留合同附件二原文，不得简写比例。
8. 中英文/代码/别名映射到同一配置时，不得判技术不一致。
9. 质保以合同5.2期限与Standard/Extended分类为主；代码不同但分类等价时不得制造BLOCKER。
10. 不得把安装地点自动当成DDP目的地。

维护中的业务规则资料（仅用于解释，不得覆盖检查状态）：
{rule_context}

确定性检查数据：
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


def normalize_contract_llm_response(data: Dict[str, Any]) -> Dict[str, Any]:
    assessment = to_str(data.get("overall_assessment", "")).strip()
    if assessment not in {"Pass", "Pass with notes", "Blocked"}:
        assessment = "Pass with notes"
    risks = data.get("key_risks") if isinstance(data.get("key_risks"), list) else []
    notes = data.get("non_blocker_issues") if isinstance(data.get("non_blocker_issues"), list) else []
    steps = data.get("next_steps") if isinstance(data.get("next_steps"), list) else []
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "overall_assessment": assessment,
        "key_risks": [
            {"risk": to_str(item.get("risk", "")), "severity": to_str(item.get("severity", "medium")), "suggestion": to_str(item.get("suggestion", ""))}
            for item in risks[:10] if isinstance(item, dict)
        ],
        "non_blocker_issues": [
            {"issue": to_str(item.get("issue", "")), "note": to_str(item.get("note", ""))}
            for item in notes[:10] if isinstance(item, dict)
        ],
        "completeness_notes": to_str(data.get("completeness_notes", "")),
        "next_steps": [to_str(step) for step in steps[:8] if to_str(step).strip()],
        "summary": truncate(data.get("summary", ""), 600),
        "confidence": max(0.0, min(1.0, confidence)),
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
            contract_data, cqp_data, ta_data, incoterm_result,
            consistency_results, warranty_result, config_result, financial_result,
        )
        parsed = extract_json_object(call_llm_chat(cfg, messages))
        normalized = normalize_contract_llm_response(parsed)
        # Enforce rule-engine authority even when the model ignores the prompt.
        has_blocker = any(bool(check.get("is_blocker")) for check in consistency_results)
        has_notes = any(str(check.get("status", "")).upper() in {"WARNING", "UNDETERMINED", "MISMATCH"} for check in consistency_results)
        normalized["overall_assessment"] = "Blocked" if has_blocker else ("Pass with notes" if has_notes else "Pass")
        normalized["knowledge_sources"] = list(get_contract_review_knowledge().source_files)
        return normalized
    except Exception as exc:
        return {
            "error": str(exc),
            "overall_assessment": "Unknown",
            "summary": f"（AI 审核失败：{truncate(str(exc), 200)}）",
        }
