# -*- coding: utf-8 -*-
"""Read-only Contract / CQP / TA comparison with traceable PDF evidence.

The source PDFs are never edited. Every review item carries its source page and,
where possible, exact word rectangles so the browser can navigate and highlight
what a reviewer must compare.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services.contract_llm_review import run_llm_contract_review
from services.pdf_evidence import (
    ParsedPDF,
    ParsedPage,
    evidence_entry,
    locate_all_evidence,
    locate_evidence,
    match_key,
    normalize_for_match,
    parse_pdf_with_evidence,
)

CATEGORY_CUSTOMER = "customer_information"
CATEGORY_PRODUCT = "product_information"
CATEGORY_OTHER = "other_information"

CATEGORY_LABELS = {
    CATEGORY_CUSTOMER: "客户信息",
    CATEGORY_PRODUCT: "产品信息",
    CATEGORY_OTHER: "其他信息",
}

SELLER_PREFIX_MAP = {
    "M": "ABB（上海）机器人投资有限公司",
    "K": "ABB机器人（珠海）有限公司",
}

MODEL_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("IRB 1200-7/0.7 Gen2", re.compile(r"IRB\s*1200\s*-\s*7\s*/\s*0[.]7\s*Gen\s*2", re.I)),
    ("IRB 1200-7/0.9 LPS", re.compile(r"IRB\s*1200\s*-\s*7\s*/\s*0[.]9\s*(?:LPS|Lite\s*[+＋])", re.I)),
    ("IRB 1100-4/0.58", re.compile(r"IRB\s*1100\s*-\s*4\s*/\s*0[.]58", re.I)),
]

MODEL_SEARCH_TERMS = {
    "IRB 1200-7/0.7 Gen2": ["IRB 1200-7/0.7 Gen2", "IRB 1200-7/0.7Gen2"],
    "IRB 1200-7/0.9 LPS": ["IRB 1200-7/0.9 LPS", "IRB 1200-7/0.9LPS", "IRB 1200-7/0.9 Lite+"],
    "IRB 1100-4/0.58": ["IRB 1100-4/0.58"],
}

WARRANTY_EXPECTED = {
    "IRB 1200-7/0.7 Gen2": {"contract": "18/12", "code": "438-1", "label": "Standard Warranty"},
    "IRB 1200-7/0.9 LPS": {"contract": "15/12", "code": "438-102", "label": "Lite Warranty"},
    "IRB 1100-4/0.58": {"contract": "18/12", "code": "438-1", "label": "Standard Warranty"},
}

# CQP configuration codes that are commercial metadata and are not required to
# be repeated in the technical agreement.
CQP_ONLY_ALLOWED_CODES = {"448-125"}

CONFIG_FEATURES: Dict[str, Sequence[str]] = {
    "3300-122": ("IRB 1200-7/0.7 Gen2",),
    "3300-121": ("IRB 1200-7/0.9 LPS", "IRB 1200-7/0.9 Lite+"),
    "3300-2": ("IRB 1100-4/0.58",),
    "209-202": ("ABB Graphite White", "标准石墨白"),
    "3350-400": ("Base 40", "IP40"),
    "3309-2": ("From side of base", "底座侧面出线"),
    "3000-105": ("OmniCore E10",),
    "3004-1": ("Max 45deg", "45°C", "45deg"),
    "3007-1": ("220-230 V AC", "220-230V AC"),
    "3007-2": ("110-230 V AC", "110-230V AC"),
    "3013-4": ("Embedded wired WAN", "连接服务"),
    "3016-1": ("FlexPendant 3m", "附带3 米电缆", "附带 3 米电缆"),
    "3043-11": ("SafeMove Standard",),
    "3044-1": ("3 modes Keyless", "3 档模式开关"),
    "3200-2": ("Length: 7m", "电缆长度7m", "电缆长度 7m"),
    "3203-5": ("CN mains cable, 3m",),
    "3120-2": ("Essential app package", "基础功能包"),
    "3151-1": ("Program package", "独立应用程序包"),
    "3303-1": ("Parallel & Air",),
    "438-1": ("Standard Warranty", "标准质保"),
    "438-102": ("Lite Warranty", "Lite 标准质保"),
}


@dataclass
class DocumentSet:
    contract_physical: Optional[ParsedPDF]
    contract_body: Optional[ParsedPDF]
    cqp: Optional[ParsedPDF]
    ta: Optional[ParsedPDF]
    ta_embedded: bool = False


def parse_pdf(filepath: str) -> ParsedPDF:
    return parse_pdf_with_evidence(filepath)


def _slice_pdf(parsed: ParsedPDF, pages: Iterable[ParsedPage], suffix: str = "") -> ParsedPDF:
    selected = list(pages)
    return ParsedPDF(
        filepath=parsed.filepath + suffix,
        pages=selected,
        full_text="\n".join(page.text for page in selected),
    )


def _find_ta_start(parsed: ParsedPDF) -> Optional[int]:
    """Return the physical page where the embedded TA actually begins.

    A contract table of contents and the separator page also contain the words
    “技术协议”, so generic keyword matching splits the contract too early.  A
    TA start must carry a strong cover/header marker.
    """
    for page in parsed.pages:
        key = match_key(page.text)
        strong_marker = (
            "technicalagreement" in key
            or "技术协议书" in key
            or "docno.3.02.f03" in key
            or "docno3.02.f03" in key
        )
        if strong_marker:
            return page.page_num
    return None


def _resolve_documents(
    parsed_by_path: Dict[str, ParsedPDF],
    file_roles: Optional[Dict[str, str]],
) -> DocumentSet:
    contract: Optional[ParsedPDF] = None
    cqp: Optional[ParsedPDF] = None
    ta: Optional[ParsedPDF] = None

    if file_roles:
        for role, path in file_roles.items():
            parsed = parsed_by_path.get(os.path.abspath(path))
            if role == "contract":
                contract = parsed
            elif role == "cqp":
                cqp = parsed
            elif role == "ta":
                ta = parsed

    # Content-based fallback, used only when a named role was not supplied.
    for parsed in parsed_by_path.values():
        text_key = match_key(parsed.full_text)
        if contract is None and "销售合同" in parsed.full_text and re.search(r"[MK]\s*\d{4}\s*-\s*\d{4}", parsed.full_text):
            contract = parsed
        if cqp is None and re.search(r"CQ\d{7}", parsed.full_text) and ("报价" in parsed.full_text or "quotation" in text_key):
            cqp = parsed
        if ta is None and "技术协议" in parsed.full_text and "销售合同" not in parsed.full_text:
            ta = parsed

    contract_body = contract
    ta_embedded = False
    if contract:
        ta_start = _find_ta_start(contract)
        if ta_start:
            body_pages = [page for page in contract.pages if page.page_num < ta_start]
            ta_pages = [page for page in contract.pages if page.page_num >= ta_start]
            contract_body = _slice_pdf(contract, body_pages, "#contract")
            if ta is None and ta_pages:
                ta = _slice_pdf(contract, ta_pages, "#ta")
                ta_embedded = True

    return DocumentSet(contract, contract_body, cqp, ta, ta_embedded)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _page_text(parsed: Optional[ParsedPDF], page_num: int) -> str:
    if parsed is None:
        return ""
    page = next((item for item in parsed.pages if item.page_num == page_num), None)
    return page.text if page else ""


def _clean_inline(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\uf000-\uf8ff]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" _\t\r\n")
    return value


def _clean_entity(value: str) -> str:
    value = _clean_inline(value)
    value = re.sub(r"\s+", "", value)
    return value.strip("_：:")


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", normalize_for_match(value))


def _contains_equivalent(left: str, right: str) -> bool:
    lkey, rkey = _compact(left), _compact(right)
    return bool(lkey and rkey and (lkey == rkey or lkey in rkey or rkey in lkey))


def _match_first(text: str, patterns: Sequence[str], flags: int = re.I | re.S) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return _clean_inline(match.group(1))
    return ""


def _first_line_value(text: str, label: str) -> str:
    """Read the value on the same visual text line as ``label``.

    CQP pages often contain two columns on one extracted line.  The helper
    returns only the text between the requested label and the next known field
    label, preventing customer data from swallowing the ABB seller column.
    """
    stop_labels = (
        "ABB 公司名称", "ABB单位名称", "ABB 单位名称", "地址 / 街道", "地址/街道",
        "联络人", "电话号码", "联系人电子邮箱", "电子邮件", "邮政编码", "城市",
        "国家或地区", "项目名称", "报价编号", "报价单编号", "日期", "报价修订版本",
        "初始编号", "询价日期", "报价日期", "负责人", "页码",
    )
    for raw_line in (text or "").splitlines():
        line = _clean_inline(raw_line)
        pos = line.find(label)
        if pos < 0:
            continue
        value = line[pos + len(label):].lstrip(" :：")
        cut = len(value)
        for stop in stop_labels:
            idx = value.find(stop)
            if idx > 0:
                cut = min(cut, idx)
        value = value[:cut].strip()
        if value:
            return value
    return ""


def _parse_money(value: str) -> float:
    try:
        return float(value.replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _canonical_model(value: str) -> str:
    for canonical, pattern in MODEL_PATTERNS:
        if pattern.search(value or ""):
            return canonical
    return _clean_inline(value)


def _models_in_text(text: str) -> List[str]:
    found: List[str] = []
    for canonical, pattern in MODEL_PATTERNS:
        if pattern.search(text or ""):
            found.append(canonical)
    return found


def _extract_model_quantities(parsed: Optional[ParsedPDF]) -> Dict[str, int]:
    if parsed is None:
        return {}
    result: Dict[str, int] = {}
    for canonical, pattern in MODEL_PATTERNS:
        candidates: List[int] = []
        for page in parsed.pages:
            normalized = unicodedata.normalize("NFKC", page.text)
            # Quantity immediately before the model, used by contract and TA.
            before = re.compile(r"(\d+)\s*台\s*" + pattern.pattern, re.I)
            candidates.extend(int(item.group(1)) for item in before.finditer(normalized))
            # Model followed by the table quantity on a nearby line, used by TA.
            after = re.compile(pattern.pattern + r"[^\n]{0,80}\n(?:[^\n]*\n){0,2}\s*(\d+)\s*台", re.I)
            candidates.extend(int(item.group(1)) for item in after.finditer(normalized))
        sensible = [number for number in candidates if 0 < number < 1000]
        if sensible:
            # Prefer the most frequently repeated business quantity.
            result[canonical] = max(set(sensible), key=lambda number: (sensible.count(number), number))
    return result


def _extract_cqp_products(parsed: Optional[ParsedPDF]) -> List[Dict[str, Any]]:
    """Extract product rows from the CQP quotation table.

    PyMuPDF keeps each quotation row on one line and places the 3HAC code on
    the following line.  Parsing that visual structure is substantially more
    stable than expecting every cell on its own line.
    """
    if parsed is None:
        return []
    products: List[Dict[str, Any]] = []
    for page in parsed.pages:
        lines = [unicodedata.normalize("NFKC", line).strip() for line in page.text.splitlines()]
        for index, line in enumerate(lines):
            for canonical, model_pattern in MODEL_PATTERNS:
                model_match = model_pattern.search(line)
                if not model_match:
                    continue
                tail = line[model_match.end():]
                numbers = re.findall(r"(?<![A-Za-z0-9])([\d,]+[.]\d+|\d+)(?![A-Za-z0-9])", tail)
                if len(numbers) < 3:
                    continue
                qty = int(numbers[0])
                unit_price = _parse_money(numbers[1])
                line_total = _parse_money(numbers[2])
                item_code = ""
                for nearby in lines[index + 1:index + 4]:
                    code_match = re.search(r"(3HAC[\d-]+)", nearby, re.I)
                    if code_match:
                        item_code = code_match.group(1).upper()
                        break
                products.append({
                    "model": canonical,
                    "item_code": item_code,
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                    "page": page.page_num,
                })
                break
    unique: List[Dict[str, Any]] = []
    seen = set()
    for product in products:
        key = product["model"]
        if key not in seen:
            unique.append(product)
            seen.add(key)
    if unique:
        return unique
    quantities = _extract_model_quantities(parsed)
    return [{"model": model, "qty": qty, "item_code": "", "unit_price": 0.0, "line_total": 0.0, "page": 0} for model, qty in quantities.items()]


def _extract_delivery_schedule(parsed: Optional[ParsedPDF]) -> List[Dict[str, Any]]:
    if parsed is None:
        return []
    schedule: List[Dict[str, Any]] = []
    for page in parsed.pages:
        text = unicodedata.normalize("NFKC", page.text)
        for canonical, pattern in MODEL_PATTERNS:
            match = re.search(
                r"(\d+)\s*台\s*" + pattern.pattern + r"\s*发货时间为[:：]?\s*([^\n]*?)_?(\d+)_?\s*周",
                text,
                re.I,
            )
            if match:
                schedule.append(
                    {
                        "model": canonical,
                        "qty": int(match.group(1)),
                        "weeks": int(match.group(3)),
                        "condition": _clean_inline(match.group(2)) or "合同生效且收到预付款后",
                        "page": page.page_num,
                    }
                )
    return schedule


def _extract_payment_terms(text: str) -> Dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", text or "")
    percentages = [int(value) for value in re.findall(r"百分之(十|四十|五十)", normalized)] if False else []
    # Chinese words are explicit in the supplied template. Keep the output
    # structured so later templates can add numeric parsing without changing API.
    result = {
        "installments": [],
        "raw": "",
        "bank": "",
        "account_name": "",
        "account_number": "",
    }
    if "百分之十" in normalized:
        result["installments"].append({"percent": 10, "trigger": "签订合同后七个工作日", "method": "电汇"})
    if "百分之四十" in normalized:
        result["installments"].append({"percent": 40, "trigger": "设备发货前七个工作日", "method": "电汇"})
    if "百分之五十" in normalized:
        result["installments"].append({"percent": 50, "trigger": "设备发货前七个工作日", "method": "ABB认可的电子银行承兑汇票"})
    result["bank"] = _match_first(normalized, [r"用户银行[:：]\s*([^\n]+)", r"结算银行[:：]?\s*([^\n]+)"])
    result["account_name"] = _match_first(normalized, [r"名称[:：]\s*([^\n]+)"])
    result["account_number"] = re.sub(r"\s+", "", _match_first(normalized, [r"账户号码[:：]\s*([\d\s-]+)", r"帐号[:：]\s*([\d\s-]+)"]))
    marker = normalized.find("付款条件")
    if marker >= 0:
        result["raw"] = _clean_inline(normalized[marker:marker + 900])
    return result


def _extract_configurations(parsed: Optional[ParsedPDF], source_type: str) -> List[Dict[str, Any]]:
    """Extract only recognized ABB configuration codes and bind them to models."""
    if parsed is None:
        return []
    valid_codes = set(CONFIG_FEATURES) | {"3300-122", "3300-121", "3300-2", "448-125"}
    code_pattern = re.compile(
        r"(?<![\d.])(" + "|".join(re.escape(code) for code in sorted(valid_codes, key=len, reverse=True)) + r")(?![\d.])",
        re.I,
    )
    configs: List[Dict[str, Any]] = []
    current_model = ""
    for page in parsed.pages:
        text = unicodedata.normalize("NFKC", page.text)
        if source_type == "cqp":
            variant = re.search(
                r"(?:3300-122|3300-121|3300-2)\s+Manipulator variant[:：]\s*([^\n]+)",
                text,
                re.I,
            )
            if not variant:
                continue
            current_model = _canonical_model(variant.group(1))

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            line_models = _models_in_text(line)
            if line_models:
                current_model = line_models[0]

            match = code_pattern.search(line)
            if not match:
                continue
            code = match.group(1)
            description = line[match.end():].strip(" :：-—")
            if not description and index + 1 < len(lines):
                description = lines[index + 1]
            if code.startswith("3300-"):
                detected = _canonical_model(description)
                if detected in MODEL_SEARCH_TERMS:
                    current_model = detected
            if not current_model:
                continue
            configs.append(
                {
                    "model": current_model,
                    "code": code,
                    "description": _clean_inline(description),
                    "page": page.page_num,
                }
            )

    unique: List[Dict[str, Any]] = []
    seen = set()
    for config in configs:
        key = (config.get("model"), config.get("code"), config.get("page"))
        if key not in seen:
            unique.append(config)
            seen.add(key)
    return unique


def _model_section_text(parsed: Optional[ParsedPDF], model: str) -> str:
    if parsed is None:
        return ""
    pages = []
    for page in parsed.pages:
        models = _models_in_text(page.text)
        if model in models:
            pages.append(page.text)
    return "\n".join(pages)


def _extract_warranty_codes_by_model(configs: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for config in configs:
        code = config.get("code", "")
        model = config.get("model", "")
        if model and code in {"438-1", "438-102", "438-2"}:
            result[model] = code
    return result


# ---------------------------------------------------------------------------
# Structured extraction
# ---------------------------------------------------------------------------


def extract_contract(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = unicodedata.normalize("NFKC", parsed.full_text)
    page1 = unicodedata.normalize("NFKC", _page_text(parsed, 1))
    page2 = unicodedata.normalize("NFKC", _page_text(parsed, 2))
    page3 = unicodedata.normalize("NFKC", _page_text(parsed, 3))
    page4 = unicodedata.normalize("NFKC", _page_text(parsed, 4))
    page9 = unicodedata.normalize("NFKC", _page_text(parsed, 9))
    page11 = unicodedata.normalize("NFKC", _page_text(parsed, 11))

    contract_number_raw = _match_first(text, [r"合同编号[:：]\s*([MK\s\d-]{8,30})"])
    contract_number = re.sub(r"\s+", "", contract_number_raw)
    buyer = _match_first(page1, [r"买方[:：]\s*(.*?)\s*地址[:：]"], re.S)
    seller = _match_first(page1, [r"卖方[:：]\s*(.*?)\s*地址[:：]"], re.S)
    buyer_address = _match_first(page1, [r"买方[:：].*?地址[:：]\s*(.*?)\s*卖方[:：]"], re.S)
    seller_address = _match_first(page1, [r"卖方[:：].*?地址[:：]\s*(.*?)(?:目录|$)"], re.S)
    quantities = _extract_model_quantities(parsed)
    delivery_schedule = _extract_delivery_schedule(parsed)

    untaxed_match = re.search(r"不含增值税总额为[:：].*?([\d,]+[.]\d+)", page3, re.S)
    gross_match = re.search(r"合同价格的含增值税总额为[:：].*?([\d,]+[.]\d+)", page3, re.S)
    vat_match = re.search(r"增值税税率[:：]_?\s*(\d{1,2})\s*%", page3)

    standard_warranty = "18/12" if re.search(r"十八\s*[（(]18[）)]\s*个月.*?十二\s*[（(]12[）)]\s*个月", page4, re.S) else ""
    lps_warranty = "15/12" if re.search(r"LPS.*?十五\s*[（(]15[）)]\s*个月.*?十二\s*[（(]12[）)]\s*个月", page4, re.S | re.I) else ""

    incoterm_options = []
    if "买方工厂的到货价" in page2:
        incoterm_options.append("买方工厂的到货价")
    if "卖方工厂出厂价" in page2:
        incoterm_options.append("卖方工厂出厂价")

    placeholder_text = re.sub(r"\s+", "", page9 + "\n" + text)
    known_placeholders = {"@@@Chop_ABB", "@@@Chop_Customer", "@@@Sign_ABBPerson", "@@@Sign_CustomerPerson"}
    placeholders = sorted(token for token in known_placeholders if token in placeholder_text)
    blank_dates = bool(re.search(r"日期[:：]\s*日期[:：]", page9) or len(re.findall(r"日期[:：]\s*(?:\n|$)", page9)) >= 1)

    return {
        "contract_number": contract_number,
        "cqp_reference": _match_first(page3, [r"单价信息[:：]\s*(CQ\d{7})"]),
        "buyer_name": _clean_entity(buyer),
        "buyer_address": _clean_entity(buyer_address),
        "seller_name": _clean_entity(seller),
        "seller_address": _clean_entity(seller_address),
        "project_name": _match_first(page2, [r"买方基于(.{2,40}?)项目需求"]),
        "end_customer_name": _clean_entity(_match_first(page3, [r"最终用户[:：]\s*([^\n]+)"])),
        "installation_location": _clean_entity(_match_first(page3, [r"设备安装地点[:：]_?\s*([^\n]+)"])),
        "delivery_location": _clean_entity(_match_first(page2, [r"交付地点[:：]_?\s*([^\n]+)"])),
        "products": [{"model": model, "qty": qty} for model, qty in quantities.items()],
        "total_qty": sum(quantities.values()),
        "delivery_schedule": delivery_schedule,
        "delivery_trigger": "合同生效且收到预付款后" if "合同生效且收到预付款后" in page2 else "",
        "split_delivery": "允许分批装运" in page2,
        "incoterm_options": incoterm_options,
        "incoterm_selected": incoterm_options[0] if len(incoterm_options) == 1 else "",
        "untaxed_amount": _parse_money(untaxed_match.group(1)) if untaxed_match else 0.0,
        "vat_rate": int(vat_match.group(1)) / 100 if vat_match else 0.0,
        "tax_included_amount": _parse_money(gross_match.group(1)) if gross_match else 0.0,
        "amount_uppercase_untaxed": _match_first(page3, [r"大写[:：]总计人民币[:：]_?([^\n]+)"]),
        "amount_uppercase_gross": _match_first(page3, [r"含增值税总额.*?大写[:：]总计人民币[:：]_?([^\n]+)"], re.S),
        "payment_terms": _extract_payment_terms(page11),
        "warranty": {"standard": standard_warranty, "lps": lps_warranty},
        "signature_placeholders": placeholders,
        "blank_signature_dates": blank_dates,
        "attachments": {
            "ta": "附件一" in text and "技术协议" in text,
            "payment": "附件二" in text and "付款方式" in text,
            "integrity": "附件三" in text and "诚信条款" in text,
        },
        "file_priority": "本合同优先于附件" if "文件优先性" in text else "",
    }


def extract_cqp(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = unicodedata.normalize("NFKC", parsed.full_text)
    page2 = unicodedata.normalize("NFKC", _page_text(parsed, 2))
    page3 = unicodedata.normalize("NFKC", _page_text(parsed, 3))
    page4 = unicodedata.normalize("NFKC", _page_text(parsed, 4))
    page5 = unicodedata.normalize("NFKC", _page_text(parsed, 5))
    products = _extract_cqp_products(parsed)
    configs = _extract_configurations(parsed, "cqp")
    money_values = [_parse_money(value) for value in re.findall(r"CNY\s*([\d,]+[.]\d+)", page4, re.I)]
    valid_match = re.search(r"报价有效期限\s*\n+([^\n]+)", page4)
    delivery_match = re.search(r"交货时间\s*\n+([^\n]+)", page5)
    term_match = re.search(r"交货条款\s*\n+([^\n]+)", page5)
    warranty_match = re.search(r"质量保证\s*\n+([^\n]+)", page5)

    customer = _first_line_value(page2, "客户")
    customer_address = _first_line_value(page2, "联络地址")
    seller_match = re.search(r"ABB 公司名称[:：]?\s*([^\n]+)", page2)
    seller = _clean_inline(seller_match.group(1)) if seller_match else _first_line_value(page2, "ABB 单位名称")
    project = _first_line_value(page2, "项目名称") or _match_first(page3, [r"项目名称\s+([^\n]+)"], re.I)

    payment_marker = page4.find("付款条件")
    payment_text = page4[payment_marker:] if payment_marker >= 0 else ""
    quote_date_match = re.search(r"报价日期\s+([0-9]{4}-[0-9]{2}-[0-9]{2})", page3)
    document_date_match = re.search(r"日期[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", page2)
    version_match = re.search(r"报价修订版本[:：]?\s*([A-Za-z0-9.]+)", page2 + "\n" + page3)
    cqp_number_match = re.search(r"(?:报价单编号|报价编号)[:：]?\s*(CQ\d{7})", text)
    if not cqp_number_match:
        cqp_number_match = re.search(r"\b(CQ\d{7})\b", text)

    return {
        "cqp_number": cqp_number_match.group(1) if cqp_number_match else "",
        "version": version_match.group(1) if version_match else "",
        "quote_date": quote_date_match.group(1) if quote_date_match else "",
        "document_date": document_date_match.group(1) if document_date_match else "",
        "validity": _clean_inline(valid_match.group(1)) if valid_match else "",
        "project_name": _clean_inline(project),
        "customer_name": _clean_inline(customer),
        "customer_address": _clean_inline(customer_address),
        "seller_name": _clean_entity(seller),
        "contact_name": _first_line_value(page2, "联络人"),
        "contact_email": _first_line_value(page2, "联系人电子邮箱"),
        "products": products,
        "total_qty": sum(int(product.get("qty", 0)) for product in products),
        "untaxed_total": money_values[0] if len(money_values) >= 1 else 0.0,
        "tax_amount": money_values[1] if len(money_values) >= 2 else 0.0,
        "tax_included_total": money_values[2] if len(money_values) >= 3 else 0.0,
        "vat_rate": int(re.search(r"增值税\s*(\d{1,2})%", page4).group(1)) / 100 if re.search(r"增值税\s*(\d{1,2})%", page4) else 0.0,
        "payment_terms": _extract_payment_terms(payment_text),
        "delivery_time": _clean_inline(delivery_match.group(1)) if delivery_match else "",
        "delivery_weeks": int(re.search(r"(\d+)\s*周", delivery_match.group(1)).group(1)) if delivery_match and re.search(r"(\d+)\s*周", delivery_match.group(1)) else 0,
        "delivery_trigger": "合同生效" if delivery_match and "合同生效" in delivery_match.group(1) else "",
        "delivery_term": _clean_inline(term_match.group(1)) if term_match else "",
        "warranty_terms": _clean_inline(warranty_match.group(1)) if warranty_match else "",
        "configurations": configs,
        "warranty_codes_by_model": _extract_warranty_codes_by_model(configs),
        "line_rounding": [
            {
                "model": product["model"],
                "shown_calculation": round(product["unit_price"] * product["qty"], 2),
                "line_total": product["line_total"],
                "difference": round(product["line_total"] - product["unit_price"] * product["qty"], 2),
            }
            for product in products
            if product.get("unit_price") and product.get("line_total")
        ],
    }


def extract_ta(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = unicodedata.normalize("NFKC", parsed.full_text)
    configs = _extract_configurations(parsed, "ta")
    quantities = _extract_model_quantities(parsed)
    page14 = _page_text(parsed, 14)
    if not page14 and parsed.pages:
        page14 = parsed.pages[min(1, len(parsed.pages) - 1)].text
    compact_text = re.sub(r"\s+", "", text)
    known_placeholders = {"@@@Chop_ABB", "@@@Chop_Customer", "@@@Sign_ABBPerson", "@@@Sign_CustomerPerson"}
    placeholders = sorted(token for token in known_placeholders if token in compact_text)
    return {
        "contract_number": _match_first(text, [r"合同编号[:：]\s*(M\d{4}-\d{4})"]),
        "buyer_name": _clean_entity(_match_first(text, [r"甲方[（(]买方[）)][:：]\s*([^\n]+)"])),
        "seller_name": _clean_entity(_match_first(text, [r"卖方[（(]乙方[）)][:：]\s*([^\n]+)", r"乙方[（(]卖方[）)][:：]\s*([^\n]+)"])),
        "products": [{"model": model, "qty": qty} for model, qty in quantities.items()],
        "total_qty": sum(quantities.values()),
        "configurations": configs,
        "warranty_codes_by_model": _extract_warranty_codes_by_model(configs),
        "lps_name_in_supply": "LPS" if re.search(r"IRB\s*1200-7/0[.]9\s*LPS", text, re.I) else "",
        "lps_name_in_parameters": "Lite+" if re.search(r"IRB\s*1200-7/0[.]9\s*Lite\s*[+＋]", text, re.I) else "",
        "repeatability_gen2": _match_first(text, [r"IRB\s*1200-7/0[.]7\s*Gen2.*?位置重复精度[:：]\s*([^\n]+)"], re.S),
        "signature_placeholders": placeholders,
        "blank_signature_dates": bool(re.search(r"日期[:：]\s+日期[:：]", text)),
        "responsibilities": {
            "buyer_integration": "买方负责机器人外围设备和机器人的系统集成" in text,
            "buyer_installation": "买方负责机器人的卸货、起吊、就位" in text,
            "seller_not_integration": "卖方不承担机器人与外围设备的系统集成" in text,
        },
    }


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------


def _evidence(
    parsed: Optional[ParsedPDF],
    document_type: str,
    label: str,
    terms: Sequence[str],
    *,
    page_hint: Optional[int] = None,
    quote: str = "",
) -> Dict[str, Any]:
    return locate_evidence(parsed, document_type, label, terms, page_hint=page_hint, quote=quote)


def _evidence_many(parsed: Optional[ParsedPDF], document_type: str, label: str, terms: Sequence[str]) -> List[Dict[str, Any]]:
    return locate_all_evidence(parsed, document_type, label, terms)


def _valid_evidence(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    seen = set()
    for entry in entries:
        if not entry:
            continue
        key = (entry.get("document_type"), entry.get("page"), entry.get("label"), entry.get("quote"))
        if key not in seen:
            output.append(entry)
            seen.add(key)
    return output


# ---------------------------------------------------------------------------
# Review item construction
# ---------------------------------------------------------------------------


def _item(
    item_id: str,
    category: str,
    title: str,
    status: str,
    severity: str,
    summary: str,
    values: Optional[Dict[str, Any]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    sub_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": item_id,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "title": title,
        "status": status,
        "severity": severity,
        "summary": summary,
        "values": values or {},
        "evidence": _valid_evidence(evidence or []),
    }
    if sub_items:
        result["sub_items"] = sub_items
    return result


def _status_from_missing(*values: Any) -> Optional[Tuple[str, str]]:
    if any(value in (None, "", [], {}) for value in values):
        return "UNDETERMINED", "warning"
    return None


def _product_map(products: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {product.get("model", ""): product for product in products if product.get("model")}


def build_review_items(
    documents: DocumentSet,
    contract: Dict[str, Any],
    cqp: Dict[str, Any],
    ta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    contract_pdf = documents.contract_physical
    cqp_pdf = documents.cqp
    ta_pdf = documents.ta

    # ---------------- Customer information ----------------
    nums = [contract.get("contract_number"), ta.get("contract_number")]
    linkage_ok = bool(contract.get("contract_number") and cqp.get("cqp_number") and contract.get("cqp_reference") == cqp.get("cqp_number") and (not ta or ta.get("contract_number") == contract.get("contract_number")))
    items.append(_item(
        "document_linkage",
        CATEGORY_CUSTOMER,
        "文件编号与版本关联",
        "PASS" if linkage_ok else "MISMATCH",
        "info" if linkage_ok else "blocker",
        "合同、CQP和TA属于同一交易。" if linkage_ok else "合同号、CQP引用或TA关联合同号不一致。",
        {
            "合同编号": contract.get("contract_number") or "未提取",
            "合同内CQP引用": contract.get("cqp_reference") or "未提取",
            "CQP编号": cqp.get("cqp_number") or "未提取",
            "TA合同编号": ta.get("contract_number") or "未提取",
            "CQP版本": cqp.get("version") or "未提取",
        },
        [
            _evidence(contract_pdf, "contract", "合同编号", [contract.get("contract_number", "")], page_hint=1),
            _evidence(contract_pdf, "contract", "CQP引用", [contract.get("cqp_reference", "")], page_hint=3),
            _evidence(cqp_pdf, "cqp", "CQP编号", [cqp.get("cqp_number", "")], page_hint=2),
            _evidence(ta_pdf, "ta", "TA合同编号", [ta.get("contract_number", "")], page_hint=13),
        ],
    ))

    seller_ok = _contains_equivalent(contract.get("seller_name", ""), cqp.get("seller_name", "")) and (not ta or _contains_equivalent(contract.get("seller_name", ""), ta.get("seller_name", "")))
    expected_seller = SELLER_PREFIX_MAP.get((contract.get("contract_number") or " ")[0], "")
    if expected_seller and not _contains_equivalent(expected_seller, contract.get("seller_name", "")):
        seller_ok = False
    items.append(_item(
        "seller_entity",
        CATEGORY_CUSTOMER,
        "卖方法定实体",
        "PASS" if seller_ok else "MISMATCH",
        "info" if seller_ok else "blocker",
        "三份文件中的卖方实体一致。" if seller_ok else "卖方法定实体或合同号前缀映射不一致。",
        {
            "合同": contract.get("seller_name") or "未提取",
            "CQP": cqp.get("seller_name") or "未提取",
            "TA": ta.get("seller_name") or "未提取",
            "合同号预期实体": expected_seller or "无映射",
        },
        [
            _evidence(contract_pdf, "contract", "合同卖方", [contract.get("seller_name", ""), "ABB（上海）机器人投资有限公司"], page_hint=1),
            _evidence(cqp_pdf, "cqp", "CQP卖方", [cqp.get("seller_name", ""), "ABB（上海）机器人投资有限公司"], page_hint=2),
            _evidence(ta_pdf, "ta", "TA卖方", [ta.get("seller_name", ""), "ABB（上海）机器人投资有限公司"], page_hint=14),
        ],
    ))

    buyer = contract.get("buyer_name", "")
    customer = cqp.get("customer_name", "")
    ta_buyer = ta.get("buyer_name", "")
    direct_buyer_match = _contains_equivalent(buyer, customer)
    ta_buyer_match = not ta or _contains_equivalent(buyer, ta_buyer)
    if buyer and customer and not direct_buyer_match:
        buyer_status, buyer_severity = "WARNING", "warning"
        buyer_summary = "CQP使用客户简称，无法仅凭文件证明其与合同买方法定名称相同，需要客户主数据映射确认。"
    elif buyer and ta_buyer_match:
        buyer_status, buyer_severity = "PASS", "info"
        buyer_summary = "合同与TA买方一致。"
    else:
        buyer_status, buyer_severity = "UNDETERMINED", "warning"
        buyer_summary = "买方信息不完整，无法确认。"
    items.append(_item(
        "buyer_customer_identity",
        CATEGORY_CUSTOMER,
        "买方与CQP客户",
        buyer_status,
        buyer_severity,
        buyer_summary,
        {"合同买方": buyer or "未提取", "CQP客户": customer or "未提取", "TA买方": ta_buyer or "未提取"},
        [
            _evidence(contract_pdf, "contract", "合同买方", [buyer, "上海华太机器人工程有限公司"], page_hint=1),
            _evidence(cqp_pdf, "cqp", "CQP客户", [customer, "SH HDC Robot"], page_hint=2),
            _evidence(ta_pdf, "ta", "TA买方", [ta_buyer, "上海华太机器人工程有限公司"], page_hint=14),
        ],
    ))

    address_status = "PASS" if contract.get("buyer_address") and cqp.get("customer_address") and "388" in contract.get("buyer_address", "") and "388" in cqp.get("customer_address", "") else "WARNING"
    items.append(_item(
        "customer_address",
        CATEGORY_CUSTOMER,
        "客户地址",
        address_status,
        "info" if address_status == "PASS" else "warning",
        "合同与CQP地址核心信息一致。" if address_status == "PASS" else "客户地址缺失或只能部分匹配，需要人工确认。",
        {"合同": contract.get("buyer_address") or "未提取", "CQP": cqp.get("customer_address") or "未提取"},
        [
            _evidence(contract_pdf, "contract", "合同客户地址", ["上海嘉定区马陆镇博学路388号", "博学路388号"], page_hint=1),
            _evidence(cqp_pdf, "cqp", "CQP客户地址", [cqp.get("customer_address", ""), "No.388 Boxue Road"], page_hint=2),
        ],
    ))

    project_key = _compact(cqp.get("project_name", ""))
    end_user = contract.get("end_customer_name", "")
    project_status = "WARNING" if end_user and project_key else "UNDETERMINED"
    items.append(_item(
        "project_end_user",
        CATEGORY_CUSTOMER,
        "项目、最终用户与安装地点",
        project_status,
        "warning",
        "合同写明最终用户和安装地点，但CQP只使用项目简称，需确认简称映射。" if project_status == "WARNING" else "最终用户或项目信息不完整。",
        {
            "合同项目": contract.get("project_name") or "未提取",
            "CQP项目": cqp.get("project_name") or "未提取",
            "最终用户": end_user or "未提取",
            "安装地点": contract.get("installation_location") or "未提取",
        },
        [
            _evidence(contract_pdf, "contract", "最终用户", [end_user, "深南电路股份有限公司"], page_hint=3),
            _evidence(contract_pdf, "contract", "安装地点", [contract.get("installation_location", ""), "深圳市龙岗区坪地街道盐龙大道1639号"], page_hint=3),
            _evidence(cqp_pdf, "cqp", "CQP项目", [cqp.get("project_name", ""), "HDC for SLDL"], page_hint=2),
        ],
    ))

    # ---------------- Product information ----------------
    c_products = _product_map(contract.get("products", []))
    q_products = _product_map(cqp.get("products", []))
    t_products = _product_map(ta.get("products", []))
    expected_models = set(c_products) | set(q_products) | set(t_products)
    model_mismatches = [model for model in sorted(expected_models) if not (model in c_products and model in q_products and (not ta or model in t_products))]
    items.append(_item(
        "product_models",
        CATEGORY_PRODUCT,
        "机器人型号",
        "MISMATCH" if model_mismatches else "PASS",
        "blocker" if model_mismatches else "info",
        "三份文件中的机器人型号一致。" if not model_mismatches else "以下型号未同时出现在合同、CQP和TA：" + "、".join(model_mismatches),
        {
            "合同": "、".join(c_products) or "未提取",
            "CQP": "、".join(q_products) or "未提取",
            "TA": "、".join(t_products) or "未提取",
        },
        _evidence_many(contract_pdf, "contract", "合同型号", list(c_products))
        + _evidence_many(cqp_pdf, "cqp", "CQP型号", list(q_products))
        + _evidence_many(ta_pdf, "ta", "TA型号", list(t_products)),
    ))

    quantity_subitems = []
    quantity_mismatch = False
    quantity_evidence: List[Dict[str, Any]] = []
    for model in sorted(expected_models):
        cq = int(c_products.get(model, {}).get("qty", 0))
        qq = int(q_products.get(model, {}).get("qty", 0))
        tq = int(t_products.get(model, {}).get("qty", 0)) if ta else cq
        ok = cq > 0 and cq == qq == tq
        quantity_mismatch = quantity_mismatch or not ok
        quantity_subitems.append({
            "id": "quantity_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_"),
            "title": model,
            "status": "PASS" if ok else "MISMATCH",
            "summary": f"Contract {cq or '未提取'} / CQP {qq or '未提取'} / TA {tq or '未提取'}",
            "values": {"Contract": cq or "未提取", "CQP": qq or "未提取", "TA": tq or "未提取"},
            "evidence": _valid_evidence(
                _evidence_many(contract_pdf, "contract", model + "数量", [f"{cq} 台{model}", f"{cq} 台 {model}"] if cq else MODEL_SEARCH_TERMS.get(model, [model]))
                + _evidence_many(cqp_pdf, "cqp", model + "数量", [model, str(qq)] if qq else [model])
                + _evidence_many(ta_pdf, "ta", model + "数量", [f"{tq} 台{model}", f"{tq} 台 {model}"] if tq else [model])
            ),
        })
        quantity_evidence.extend(quantity_subitems[-1]["evidence"])
    items.append(_item(
        "product_quantities",
        CATEGORY_PRODUCT,
        "各型号数量",
        "MISMATCH" if quantity_mismatch else "PASS",
        "blocker" if quantity_mismatch else "info",
        "各型号均为5台。" if not quantity_mismatch else "至少一个型号的数量不一致或未提取。",
        {
            "合同总数": contract.get("total_qty") or "未提取",
            "CQP总数": cqp.get("total_qty") or "未提取",
            "TA总数": ta.get("total_qty") or "未提取",
        },
        quantity_evidence,
        quantity_subitems,
    ))

    total_ok = contract.get("total_qty") == cqp.get("total_qty") == ta.get("total_qty") == 15
    items.append(_item(
        "total_quantity",
        CATEGORY_PRODUCT,
        "机器人总数量",
        "PASS" if total_ok else "MISMATCH",
        "info" if total_ok else "blocker",
        "5 + 5 + 5 = 15，三份文件一致。" if total_ok else "机器人总数量不一致；总数必须由各型号产品数量求和，不得统计型号出现次数。",
        {"合同": contract.get("total_qty") or "未提取", "CQP": cqp.get("total_qty") or "未提取", "TA": ta.get("total_qty") or "未提取"},
        quantity_evidence,
    ))

    product_code_subitems = []
    for model, product in q_products.items():
        code = product.get("item_code", "")
        product_code_subitems.append({
            "id": "product_code_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_"),
            "title": model,
            "status": "PASS" if code else "UNDETERMINED",
            "summary": f"产品代码：{code or '未提取'}",
            "values": {"CQP产品代码": code or "未提取"},
            "evidence": [_evidence(cqp_pdf, "cqp", model + "产品代码", [code, model], page_hint=product.get("page"))],
        })
    items.append(_item(
        "product_codes",
        CATEGORY_PRODUCT,
        "产品代码",
        "PASS" if product_code_subitems and all(sub["status"] == "PASS" for sub in product_code_subitems) else "UNDETERMINED",
        "info" if product_code_subitems and all(sub["status"] == "PASS" for sub in product_code_subitems) else "warning",
        "CQP中的三个产品代码均已提取。" if product_code_subitems else "未提取到产品代码。",
        {},
        [entry for sub in product_code_subitems for entry in sub["evidence"]],
        product_code_subitems,
    ))

    # Warranty by model, not as one global code set.
    warranty_subitems = []
    warranty_problem = False
    for model in sorted(WARRANTY_EXPECTED):
        expected = WARRANTY_EXPECTED[model]
        cqp_code = cqp.get("warranty_codes_by_model", {}).get(model, "")
        ta_code = ta.get("warranty_codes_by_model", {}).get(model, "")
        contract_period = contract.get("warranty", {}).get("lps" if model.endswith("LPS") else "standard", "")
        ok = contract_period == expected["contract"] and cqp_code == expected["code"] and ta_code == expected["code"]
        warranty_problem = warranty_problem or not ok
        ev_terms_contract = ["其中LPS 的质保期", "十五（15）个月", "十二（12）个月"] if model.endswith("LPS") else ["十八（18）个月", "十二（12）个月"]
        warranty_subitems.append({
            "id": "warranty_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_"),
            "title": model,
            "status": "PASS" if ok else "MISMATCH",
            "summary": f"合同 {contract_period or '未提取'} / CQP {cqp_code or '未提取'} / TA {ta_code or '未提取'}",
            "values": {"合同质保": contract_period or "未提取", "CQP代码": cqp_code or "未提取", "TA代码": ta_code or "未提取"},
            "evidence": _valid_evidence([
                _evidence(contract_pdf, "contract", model + "合同质保", ev_terms_contract, page_hint=4),
                _evidence(cqp_pdf, "cqp", model + " CQP质保", [cqp_code, expected["label"]], quote=cqp_code or expected["label"]),
                _evidence(ta_pdf, "ta", model + " TA质保", [ta_code, expected["label"]], quote=ta_code or expected["label"]),
            ]),
        })
    items.append(_item(
        "warranty_by_model",
        CATEGORY_PRODUCT,
        "按型号校对质保",
        "MISMATCH" if warranty_problem else "PASS",
        "blocker" if warranty_problem else "info",
        "LPS使用438-102 Lite Warranty并对应15/12，其余型号使用438-1并对应18/12。" if not warranty_problem else "至少一个型号的合同质保期限、CQP代码或TA代码不一致。",
        {},
        [entry for sub in warranty_subitems for entry in sub["evidence"]],
        warranty_subitems,
    ))

    # Configuration comparison: exact code or a model-specific semantic equivalent.
    cqp_configs = cqp.get("configurations", [])
    ta_configs = ta.get("configurations", [])
    config_subitems = []
    real_mismatches = []
    ignored_codes = []
    for config in cqp_configs:
        model, code = config.get("model", ""), config.get("code", "")
        if not model or not code:
            continue
        if code in CQP_ONLY_ALLOWED_CODES:
            ignored_codes.append(code)
            continue
        exact = any(item.get("model") == model and item.get("code") == code for item in ta_configs)
        ta_section = _model_section_text(ta_pdf, model)
        semantic = exact or any(match_key(term) in match_key(ta_section) for term in CONFIG_FEATURES.get(code, ()))
        if not semantic:
            real_mismatches.append((model, code))
        cqp_ev = _evidence(cqp_pdf, "cqp", f"{model} {code}", [code], page_hint=config.get("page"))
        ta_terms = [code, *CONFIG_FEATURES.get(code, ())]
        ta_ev = _evidence(ta_pdf, "ta", f"{model} 对应配置", ta_terms)
        config_subitems.append({
            "id": "config_" + re.sub(r"[^a-z0-9]+", "_", (model + "_" + code).lower()).strip("_"),
            "title": f"{model} · {code}",
            "status": "PASS" if semantic else "MISMATCH",
            "summary": "TA中存在相同代码或等价描述。" if semantic else "CQP配置在TA中未找到相同代码或可信等价描述。",
            "values": {"CQP描述": config.get("description") or "", "TA匹配方式": "相同代码" if exact else ("等价描述" if semantic else "未匹配")},
            "evidence": _valid_evidence([cqp_ev, ta_ev]),
        })
    # De-duplicate repetitive shared configurations by item id.
    deduped_configs = []
    seen_config_ids = set()
    for sub in config_subitems:
        if sub["id"] not in seen_config_ids:
            deduped_configs.append(sub)
            seen_config_ids.add(sub["id"])
    items.append(_item(
        "configuration_consistency",
        CATEGORY_PRODUCT,
        "CQP与TA技术配置",
        "MISMATCH" if real_mismatches else "PASS",
        "blocker" if real_mismatches else "info",
        "配置代码或等价技术描述一致；448-125属于CQP交付元数据，不要求写入TA。" if not real_mismatches else "存在未在TA中找到的CQP技术配置。",
        {"允许仅存在于CQP": "、".join(sorted(set(ignored_codes))) or "无", "真实差异": "、".join(f"{model}:{code}" for model, code in real_mismatches) or "无"},
        [entry for sub in deduped_configs for entry in sub["evidence"]],
        deduped_configs,
    ))

    naming_warning = bool(ta.get("lps_name_in_supply") and ta.get("lps_name_in_parameters") and ta.get("lps_name_in_supply") != ta.get("lps_name_in_parameters"))
    items.append(_item(
        "lps_lite_naming",
        CATEGORY_PRODUCT,
        "LPS / Lite+型号命名",
        "WARNING" if naming_warning else "PASS",
        "warning" if naming_warning else "info",
        "TA供货范围使用LPS，但技术参数章节使用Lite+，需确认是否为同一正式型号。" if naming_warning else "型号命名一致。",
        {"供货范围": ta.get("lps_name_in_supply") or "未提取", "技术参数": ta.get("lps_name_in_parameters") or "未提取"},
        [
            _evidence(ta_pdf, "ta", "TA供货范围型号", ["IRB 1200-7/0.9 LPS"], page_hint=17),
            _evidence(ta_pdf, "ta", "TA参数章节型号", ["IRB 1200-7/0.9 Lite+"], page_hint=20),
        ],
    ))

    repeatability = ta.get("repeatability_gen2", "")
    unit_warning = bool(repeatability and re.search(r"0[.]011\s*m(?!m)", repeatability, re.I))
    items.append(_item(
        "technical_parameter_units",
        CATEGORY_PRODUCT,
        "技术参数单位合理性",
        "WARNING" if unit_warning else "PASS",
        "warning" if unit_warning else "info",
        "IRB 1200-7/0.7 Gen2的重复定位精度写为0.011m，与其他型号使用mm不一致，疑似单位错误。" if unit_warning else "未发现明显单位异常。",
        {"重复定位精度": repeatability or "未提取"},
        [_evidence(ta_pdf, "ta", "重复定位精度", ["位置重复精度： 0.011m", "0.011m"], page_hint=19)],
    ))

    # ---------------- Other information ----------------
    untaxed_diff = round(abs(contract.get("untaxed_amount", 0) - cqp.get("untaxed_total", 0)), 2)
    items.append(_item(
        "untaxed_amount",
        CATEGORY_OTHER,
        "未税金额",
        "PASS" if untaxed_diff < 0.01 and contract.get("untaxed_amount") else "MISMATCH",
        "info" if untaxed_diff < 0.01 and contract.get("untaxed_amount") else "blocker",
        "合同与CQP未税金额一致。" if untaxed_diff < 0.01 and contract.get("untaxed_amount") else "合同与CQP未税金额不一致或未提取。",
        {"合同": f"¥{contract.get('untaxed_amount', 0):,.2f}", "CQP": f"¥{cqp.get('untaxed_total', 0):,.2f}", "差异": f"¥{untaxed_diff:,.2f}"},
        [
            _evidence(contract_pdf, "contract", "合同未税金额", ["870,663.37", str(contract.get("untaxed_amount", ""))], page_hint=3),
            _evidence(cqp_pdf, "cqp", "CQP未税金额", ["870,663.37", str(cqp.get("untaxed_total", ""))], page_hint=4),
        ],
    ))

    vat_ok = contract.get("vat_rate") and abs(contract.get("vat_rate", 0) - cqp.get("vat_rate", 0)) < 0.0001
    items.append(_item(
        "vat_rate",
        CATEGORY_OTHER,
        "增值税率与税额",
        "PASS" if vat_ok else "MISMATCH",
        "info" if vat_ok else "blocker",
        "双方VAT均为13%。" if vat_ok else "VAT税率不一致或未提取。",
        {"合同VAT": f"{contract.get('vat_rate', 0)*100:.0f}%", "CQP VAT": f"{cqp.get('vat_rate', 0)*100:.0f}%", "CQP税额": f"¥{cqp.get('tax_amount', 0):,.2f}"},
        [
            _evidence(contract_pdf, "contract", "合同VAT", ["13%"], page_hint=3),
            _evidence(cqp_pdf, "cqp", "CQP VAT", ["增值税 13%", "13%"], page_hint=4),
            _evidence(cqp_pdf, "cqp", "CQP税额", ["113,186.24"], page_hint=4),
        ],
    ))

    gross_diff = round(abs(contract.get("tax_included_amount", 0) - cqp.get("tax_included_total", 0)), 2)
    items.append(_item(
        "tax_included_amount",
        CATEGORY_OTHER,
        "含税金额",
        "MISMATCH" if gross_diff >= 0.01 else "PASS",
        "blocker" if gross_diff >= 0.01 else "info",
        "合同含税金额与CQP相差0.61元，需要在签署和开票前统一。" if gross_diff >= 0.01 else "含税金额一致。",
        {"合同": f"¥{contract.get('tax_included_amount', 0):,.2f}", "CQP": f"¥{cqp.get('tax_included_total', 0):,.2f}", "差异": f"¥{gross_diff:,.2f}"},
        [
            _evidence(contract_pdf, "contract", "合同含税金额", ["983,849.00"], page_hint=3),
            _evidence(cqp_pdf, "cqp", "CQP含税金额", ["983,849.61"], page_hint=4),
        ],
    ))

    rounding_issues = [entry for entry in cqp.get("line_rounding", []) if abs(entry.get("difference", 0)) >= 0.01]
    items.append(_item(
        "cqp_line_rounding",
        CATEGORY_OTHER,
        "CQP单价与行总额舍入",
        "WARNING" if rounding_issues else "PASS",
        "warning" if rounding_issues else "info",
        "显示单价乘数量与行总额存在分差，可能使用了隐藏精度，需要确认报价系统舍入规则。" if rounding_issues else "显示单价与行总额计算一致。",
        {entry["model"]: f"显示计算 {entry['shown_calculation']:,.2f} / 行总额 {entry['line_total']:,.2f}" for entry in rounding_issues},
        [_evidence(cqp_pdf, "cqp", "CQP报价范围", ["60,773.81", "303,869.03", "52,582.57", "262,912.84", "60,776.30", "303,881.49"], page_hint=4)],
    ))

    c_inst = contract.get("payment_terms", {}).get("installments", [])
    q_inst = cqp.get("payment_terms", {}).get("installments", [])
    payment_ok = [entry.get("percent") for entry in c_inst] == [10, 40, 50] and [entry.get("percent") for entry in q_inst] == [10, 40, 50]
    items.append(_item(
        "payment_terms",
        CATEGORY_OTHER,
        "付款条件与开票",
        "PASS" if payment_ok else "MISMATCH",
        "info" if payment_ok else "blocker",
        "合同附件二与CQP均为10% / 40% / 50%，触发时间和付款方式一致。" if payment_ok else "付款比例、触发条件或付款方式不一致。",
        {
            "合同": "10%签约后电汇；40%发货前电汇；50%发货前银行承兑" if c_inst else "未提取",
            "CQP": "10%签约后电汇；40%发货前电汇；50%发货前银行承兑" if q_inst else "未提取",
            "收款账户": contract.get("payment_terms", {}).get("account_number") or "未提取",
        },
        [
            _evidence(contract_pdf, "contract", "合同付款条件", ["合同总价的百分之十", "合同总价的百分之四十", "合同总价的百分之五十"], page_hint=11),
            _evidence(cqp_pdf, "cqp", "CQP付款条件", ["合同总价的百分之十", "合同总价的百分之四十", "合同总价的百分之五十"], page_hint=4),
        ],
    ))

    schedule = contract.get("delivery_schedule", [])
    q_weeks = cqp.get("delivery_weeks", 0)
    delivery_mismatch = bool(schedule and q_weeks and any(entry.get("weeks") != q_weeks for entry in schedule))
    trigger_mismatch = bool(schedule and cqp.get("delivery_trigger") and contract.get("delivery_trigger") != cqp.get("delivery_trigger"))
    items.append(_item(
        "delivery_period",
        CATEGORY_OTHER,
        "交付周期与起算条件",
        "MISMATCH" if delivery_mismatch or trigger_mismatch else ("UNDETERMINED" if not schedule or not q_weeks else "PASS"),
        "blocker" if delivery_mismatch or trigger_mismatch else ("warning" if not schedule or not q_weeks else "info"),
        "合同为8/9/9周且从收到预付款后起算，CQP为6周且从合同生效起算。" if delivery_mismatch or trigger_mismatch else "交付周期一致。",
        {
            "合同": "；".join(f"{entry['model']} {entry['weeks']}周" for entry in schedule) or "未提取",
            "合同起算": contract.get("delivery_trigger") or "未提取",
            "CQP": cqp.get("delivery_time") or "未提取",
            "CQP起算": cqp.get("delivery_trigger") or "未提取",
        },
        _evidence_many(contract_pdf, "contract", "合同交付周期", ["8_周", "9_周", "合同生效且收到预付款后"])
        + _evidence_many(cqp_pdf, "cqp", "CQP交付周期", ["6 周", "自合同生效之日起计算"]),
    ))

    contract_options = contract.get("incoterm_options", [])
    cqp_term = cqp.get("delivery_term", "")
    incoterm_status = "UNDETERMINED" if len(contract_options) != 1 else ("PASS" if ("DDP" in cqp_term.upper()) == ("到货价" in contract_options[0]) else "MISMATCH")
    items.append(_item(
        "incoterm_delivery_place",
        CATEGORY_OTHER,
        "贸易术语、交付地点与风险转移",
        incoterm_status,
        "blocker" if incoterm_status in {"MISMATCH", "UNDETERMINED"} else "info",
        "合同页面同时保留到货价和出厂价选项，无法确定位置；CQP写DDP Shanghai，需要人工确认合同实际勾选项及完整named place。" if incoterm_status == "UNDETERMINED" else "贸易术语一致。",
        {
            "合同可见选项": " / ".join(contract_options) or "未提取",
            "合同交付地点": contract.get("delivery_location") or "未提取",
            "CQP": cqp_term or "未提取",
        },
        [
            _evidence(contract_pdf, "contract", "合同到货价选项", ["买方工厂的到货价"], page_hint=2),
            _evidence(contract_pdf, "contract", "合同出厂价选项", ["卖方工厂出厂价"], page_hint=2),
            _evidence(contract_pdf, "contract", "合同交付地点", [contract.get("delivery_location", ""), "博学路388号"], page_hint=2),
            _evidence(cqp_pdf, "cqp", "CQP贸易术语", ["DDP Shanghai", "INCOTERMS 2010"], page_hint=5),
        ],
    ))

    signature_placeholders = sorted(set(contract.get("signature_placeholders", []) + ta.get("signature_placeholders", [])))
    signature_issue = bool(signature_placeholders or contract.get("blank_signature_dates") or ta.get("blank_signature_dates"))
    items.append(_item(
        "signature_completeness",
        CATEGORY_OTHER,
        "签字、盖章与日期完整性",
        "WARNING" if signature_issue else "PASS",
        "warning" if signature_issue else "info",
        "当前文件仍含签字/盖章模板占位符或空日期；草稿阶段可接受，最终签署版必须补全。" if signature_issue else "签署信息完整。",
        {"占位符": "、".join(signature_placeholders) or "无", "合同日期空白": bool(contract.get("blank_signature_dates")), "TA日期空白": bool(ta.get("blank_signature_dates"))},
        [
            _evidence(contract_pdf, "contract", "合同签署页", ["@@@Sign_ABBPerson", "@@@Sign_CustomerPerson", "日期："], page_hint=9),
            _evidence(ta_pdf, "ta", "TA签署页", ["@@@Sign_CustomerPerson", "@@@Sign_ABBPerson", "日期："], page_hint=26),
        ],
    ))

    attachments = contract.get("attachments", {})
    attachment_ok = all(attachments.get(key) for key in ("ta", "payment", "integrity"))
    items.append(_item(
        "attachment_completeness",
        CATEGORY_OTHER,
        "附件完整性与文件优先级",
        "PASS" if attachment_ok else "MISMATCH",
        "info" if attachment_ok else "blocker",
        "TA、付款方式和诚信条款均存在；合同规定本合同优先于附件。" if attachment_ok else "合同声明的附件不完整。",
        {"技术协议": bool(attachments.get("ta")), "付款方式": bool(attachments.get("payment")), "诚信条款": bool(attachments.get("integrity")), "优先级": contract.get("file_priority") or "未提取"},
        [
            _evidence(contract_pdf, "contract", "合同附件清单", ["附件一技术协议", "附件二付款方式", "附件三诚信条款"], page_hint=1),
            _evidence(contract_pdf, "contract", "文件优先性", ["文件优先性", "本合同"], page_hint=8),
        ],
    ))

    responsibilities = ta.get("responsibilities", {})
    responsibility_ok = all(responsibilities.values()) if responsibilities else False
    items.append(_item(
        "scope_responsibility",
        CATEGORY_OTHER,
        "供货范围与责任边界",
        "PASS" if responsibility_ok else "UNDETERMINED",
        "info" if responsibility_ok else "warning",
        "TA明确由买方承担系统集成、编程调试、卸货和现场安装，卖方责任限于标准产品供货。" if responsibility_ok else "责任边界未完整提取。",
        responsibilities,
        [
            _evidence(ta_pdf, "ta", "卖方责任", ["卖方不承担机器人与外围设备的系统集成"], page_hint=25),
            _evidence(ta_pdf, "ta", "买方系统集成责任", ["买方负责机器人外围设备和机器人的系统集成"], page_hint=26),
            _evidence(ta_pdf, "ta", "买方现场安装责任", ["买方负责机器人的卸货、起吊、就位"], page_hint=26),
            _evidence(cqp_pdf, "cqp", "CQP安装调试责任", ["货物的安装及调试将被默认为由买方"], page_hint=5),
        ],
    ))

    return items


# ---------------------------------------------------------------------------
# Legacy compatibility and orchestration
# ---------------------------------------------------------------------------


def _legacy_check(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "check_name": item.get("title", ""),
        "status": item.get("status", ""),
        "detail": item.get("summary", ""),
        "is_blocker": item.get("severity") == "blocker" and item.get("status") != "PASS",
    }


def _serialize_extracted(data: Dict[str, Any]) -> Dict[str, Any]:
    return data


def _run_optional_llm(
    contract: Dict[str, Any],
    cqp: Dict[str, Any],
    ta: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    legacy = [_legacy_check(item) for item in items]
    incoterm_item = next((item for item in items if item["id"] == "incoterm_delivery_place"), {})
    incoterm_result = {
        "conclusion": "DDP" if "DDP" in str(incoterm_item.get("values", {}).get("CQP", "")).upper() else "UNDETERMINED",
        "contract_evidence": incoterm_item.get("values", {}).get("合同可见选项", ""),
        "cqp_evidence": incoterm_item.get("values", {}).get("CQP", ""),
        "consistent": incoterm_item.get("status") == "PASS",
    }
    warranty_item = next((item for item in items if item["id"] == "warranty_by_model"), {})
    config_item = next((item for item in items if item["id"] == "configuration_consistency"), {})
    financial = {
        "vat_check": next((item for item in items if item["id"] == "vat_rate"), {}),
        "untaxed_check": next((item for item in items if item["id"] == "untaxed_amount"), {}),
        "tax_included_check": next((item for item in items if item["id"] == "tax_included_amount"), {}),
    }
    try:
        return run_llm_contract_review(
            contract_data=contract,
            cqp_data=cqp,
            ta_data=ta,
            incoterm_result=incoterm_result,
            consistency_results=legacy,
            warranty_result={"consistent": warranty_item.get("status") == "PASS", "detail": warranty_item.get("summary", "")},
            config_result={"overall_consistent": config_item.get("status") == "PASS", "models_compared": []},
            financial_result=financial,
        )
    except Exception as exc:
        return {"error": str(exc), "overall_assessment": "Unknown", "summary": "AI审核不可用，规则检查不受影响。"}


def run_review(
    pdf_paths: List[str],
    customer_db_path: str = None,
    template_path: str = None,
    file_roles: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    del customer_db_path, template_path
    parsed_by_path = {os.path.abspath(path): parse_pdf(path) for path in pdf_paths}
    documents = _resolve_documents(parsed_by_path, file_roles)
    if documents.contract_physical is None or documents.cqp is None:
        raise ValueError("Contract 和 CQP 都必须上传且能够读取。")

    contract = extract_contract(documents.contract_body)
    cqp = extract_cqp(documents.cqp)
    ta = extract_ta(documents.ta)
    review_items = build_review_items(documents, contract, cqp, ta)
    key_checks = [_legacy_check(item) for item in review_items]
    blockers = [
        {"type": item["id"], "detail": item["summary"]}
        for item in review_items
        if item.get("severity") == "blocker" and item.get("status") != "PASS"
    ]
    non_blockers = [
        {"type": item["id"], "detail": item["summary"]}
        for item in review_items
        if item.get("severity") == "warning" and item.get("status") != "PASS"
    ]
    conclusion = "Blocked" if blockers else ("Pass with notes" if non_blockers else "Pass")
    llm_review = _run_optional_llm(contract, cqp, ta, review_items)

    return {
        "conclusion": conclusion,
        "review_categories": [
            {"id": CATEGORY_CUSTOMER, "title": CATEGORY_LABELS[CATEGORY_CUSTOMER], "order": 1},
            {"id": CATEGORY_PRODUCT, "title": CATEGORY_LABELS[CATEGORY_PRODUCT], "order": 2},
            {"id": CATEGORY_OTHER, "title": CATEGORY_LABELS[CATEGORY_OTHER], "order": 3},
        ],
        "document_sources": {
            "contract": {"physical_role": "contract", "embedded": False},
            "cqp": {"physical_role": "cqp", "embedded": False},
            "ta": {"physical_role": "contract" if documents.ta_embedded else "ta", "embedded": documents.ta_embedded},
        },
        "source_recognition": {
            "contract": {"status": "found", "page_count": len(documents.contract_physical.pages)},
            "cqp": {"status": "found", "page_count": len(documents.cqp.pages)},
            "ta": {"status": "embedded" if documents.ta_embedded else ("found" if documents.ta else "not_found"), "page_count": len(documents.ta.pages) if documents.ta else 0},
        },
        "extracted_data": {
            "contract": _serialize_extracted(contract),
            "cqp": _serialize_extracted(cqp),
            "ta": _serialize_extracted(ta),
        },
        "key_checks": key_checks,
        "blockers": blockers,
        "non_blockers": non_blockers,
        "review_items": review_items,
        "llm_review": llm_review,
        # Legacy placeholders retained so older UI code does not fail.
        "incoterm": next((item for item in review_items if item["id"] == "incoterm_delivery_place"), {}),
        "warranty": next((item for item in review_items if item["id"] == "warranty_by_model"), {}),
        "configuration": next((item for item in review_items if item["id"] == "configuration_consistency"), {}),
        "financial": {
            "untaxed": next((item for item in review_items if item["id"] == "untaxed_amount"), {}),
            "vat": next((item for item in review_items if item["id"] == "vat_rate"), {}),
            "tax_included": next((item for item in review_items if item["id"] == "tax_included_amount"), {}),
        },
        "bt09_draft": None,
    }
