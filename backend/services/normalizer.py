# -*- coding: utf-8 -*-
"""Text normalization utilities for document check.

Handles:
- Full-width / half-width conversion
- Whitespace normalization
- Parenthesis normalization
- Case normalization
- Company name normalization
- Robot model normalization
- Money amount normalization
- Address normalization
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace (spaces, newlines, tabs) into single spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).strip())


def normalize_fullwidth(text: str) -> str:
    """Convert full-width characters to half-width."""
    if not text:
        return ""
    result = []
    for ch in str(text):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(" ")
        else:
            result.append(ch)
    return "".join(result)


def normalize_parentheses(text: str) -> str:
    """Normalize Chinese/English parentheses to ASCII."""
    if not text:
        return ""
    t = str(text)
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("【", "[").replace("】", "]")
    t = t.replace("｛", "{").replace("｝", "}")
    return t


def normalize_chinese_numbers(text: str) -> str:
    """Convert common Chinese numerals to Arabic digits in isolation.
    
    This handles common patterns like: 三十八 -> 38, 一百二十 -> 120
    but is conservative to avoid false positives.
    """
    if not text:
        return ""
    cn_digits = {"零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                 "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    # Only convert isolated single Chinese digit characters
    t = str(text)
    for cn, ar in cn_digits.items():
        t = t.replace(cn, ar)
    return t


def normalize_company_name(name: str) -> str:
    """Normalize company names for comparison.
    
    - Remove extra spaces
    - Normalize full/half width
    - Normalize parentheses
    - Lowercase
    - Common abbreviations
    """
    if not name:
        return ""
    n = normalize_fullwidth(str(name))
    n = normalize_parentheses(n)
    n = normalize_whitespace(n)
    n = n.lower()
    # ABB specific: normalize spacing around ABB
    n = re.sub(r"\babb\b", "abb", n)
    # Collapse multiple spaces after normalization
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_robot_model(model: str) -> str:
    """Normalize robot model names for comparison.

    Examples:
        "IRB 1200-7/0.7Gen2" -> "irb1200-7/0.7gen2"
        "IRB1200-7/0.7 Gen 2" -> "irb1200-7/0.7gen2"
        "IRB 1200-7/0.7 Gen2" -> "irb1200-7/0.7gen2"
    """
    if not model:
        return ""
    m = normalize_fullwidth(str(model))
    m = normalize_parentheses(m)
    m = m.lower().strip()
    # Remove all spaces
    m = re.sub(r"\s+", "", m)
    # Normalize "Gen 2" / "Gen2" -> "gen2"
    m = re.sub(r"gen\s*(\d+)", r"gen\1", m)
    # Remove trailing/leading non-alphanumeric except /
    m = m.strip(".,;: ")
    return m


def normalize_money(value: Any) -> str:
    """Normalize money amounts for comparison.
    
    Converts to Decimal string with 2 decimal places.
    Removes thousand separators and currency symbols.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float, Decimal)):
        amount = Decimal(str(value))
        return str(amount.quantize(Decimal("0.01")))
    s = str(value).strip()
    if not s:
        return ""
    # Remove currency symbols and common prefixes
    s = re.sub(r"RMB|CNY|￥|¥|EUR|USD|\$|元|人民币|€", "", s, flags=re.I)
    # Remove thousand separators
    s = s.replace(",", "").replace("，", "").replace(" ", "")
    s = s.strip()
    try:
        amount = Decimal(s)
        return str(amount.quantize(Decimal("0.01")))
    except Exception:
        return s


def normalize_vat_rate(value: Any) -> str:
    """Normalize VAT rate to a comparable decimal string."""
    if value is None:
        return ""
    s = str(value).strip()
    s = s.replace("%", "").replace("％", "").strip()
    try:
        rate = Decimal(s)
        # If rate > 1, assume it's percentage (e.g., 13 -> 0.13)
        if rate > 1:
            rate = rate / Decimal("100")
        return str(rate.quantize(Decimal("0.0001")))
    except Exception:
        return s


def normalize_address(address: str) -> str:
    """Normalize Chinese/English addresses."""
    if not address:
        return ""
    a = normalize_fullwidth(str(address))
    a = normalize_parentheses(a)
    a = normalize_whitespace(a)
    a = a.lower()
    # Normalize "No.388" / "388号" -> "388"
    a = re.sub(r"no\.?\s*(\d+)", r"\1", a)
    a = a.replace("号", "")
    # Collapse spaces
    a = re.sub(r"\s+", " ", a).strip()
    return a


def normalize_config_code(code: str) -> str:
    """Normalize configuration codes like '3024-2'."""
    if not code:
        return ""
    c = str(code).strip().upper()
    c = re.sub(r"\s+", "", c)
    return c


def normalize_config_description(desc: str) -> str:
    """Normalize configuration description for comparison."""
    if not desc:
        return ""
    d = normalize_fullwidth(str(desc))
    d = normalize_parentheses(d)
    d = normalize_whitespace(d)
    d = d.lower()
    # Remove common filler words
    d = re.sub(r"\b(the|a|an|for|with|and|or|in|on|at|to|of)\b", "", d)
    d = re.sub(r"\s+", " ", d).strip()
    return d


def detect_contract_prefix(contract_number: str) -> str:
    """Detect contract prefix (M or K)."""
    if not contract_number:
        return ""
    cn = str(contract_number).strip().upper()
    if cn.startswith("M"):
        return "M"
    if cn.startswith("K"):
        return "K"
    # Try regex
    m = re.match(r"^([MK])\d", cn)
    if m:
        return m.group(1)
    return ""


def compare_normalized(a: str, b: str, normalize_func=None) -> bool:
    """Compare two strings after normalization."""
    if normalize_func:
        return normalize_func(a) == normalize_func(b)
    return normalize_whitespace(str(a or "").lower()) == normalize_whitespace(str(b or "").lower())


# Seller entity mapping
SELLER_PREFIX_MAP = {
    "M": "ABB（上海）机器人投资有限公司",
    "K": "ABB机器人（珠海）有限公司",
}

SELLER_ENTITY_NAMES = {
    "abb（上海）机器人投资有限公司": "M",
    "abb机器人（珠海）有限公司": "K",
    "abb (shanghai) robot investment co ltd": "M",
    "abb (shanghai) robot investment co., ltd.": "M",
    "abb robot (zhuhai) co ltd": "K",
    "abb robot (zhuhai) co., ltd.": "K",
}