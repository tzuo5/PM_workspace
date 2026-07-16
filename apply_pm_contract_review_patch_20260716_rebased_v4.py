#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply the rebased 2026-07-16 PM_workspace contract-review regression fixes (V4).

Run from the repository root:
    python apply_pm_contract_review_patch_20260716_rebased_v4.py

The script:
- refuses to overwrite dirty target files unless --force is supplied;
- creates timestamped backups under .pm_patch_backups/;
- does not commit, stage, reset, or checkout anything;
- is idempotent;
- compiles changed Python files and runs focused tests by default.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple

PATCH_ID = "PM_CONTRACT_REVIEW_PATCH_20260716_V4"
EXPECTED_BASE_COMMIT = "cb91c5dd5ab5533adb3c58cd8ffbecc887df1c3a"
TARGETS = (
    "backend/services/pdf_evidence.py",
    "backend/services/contract_review_knowledge.py",
    "backend/services/contract_review_engine.py",
    "backend/services/llm_console.py",
    "backend/server.py",
    "backend/tests/test_contract_review.py",
)
NEW_TEST = "backend/tests/test_contract_review_regressions_20260716.py"


def _run(
    cmd: List[str],
    cwd: Path,
    check: bool = True,
    env: Dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def find_repo_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / "backend/services/contract_review_engine.py").is_file():
            return candidate
    raise SystemExit("未找到 PM_workspace 根目录。请在仓库根目录运行，或使用 --root 指定路径。")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".pm_patch_tmp")
    temp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def top_level_node(source: str, name: str, kinds: Tuple[type, ...]) -> ast.AST | None:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, kinds) and getattr(node, "name", None) == name:
            return node
    return None


def replace_top_level_function(source: str, name: str, replacement: str) -> str:
    node = top_level_node(source, name, (ast.FunctionDef, ast.AsyncFunctionDef))
    if node is None or not getattr(node, "end_lineno", None):
        raise RuntimeError(f"找不到顶层函数：{name}")
    lines = source.splitlines(keepends=True)
    desired = textwrap.dedent(replacement).strip()
    current = "".join(lines[node.lineno - 1 : node.end_lineno]).strip()
    if current == desired:
        return source
    block = desired + "\n\n"
    lines[node.lineno - 1 : node.end_lineno] = [block]
    return "".join(lines)


def replace_top_level_class(source: str, name: str, replacement: str) -> str:
    node = top_level_node(source, name, (ast.ClassDef,))
    if node is None or not getattr(node, "end_lineno", None):
        raise RuntimeError(f"找不到顶层类：{name}")
    lines = source.splitlines(keepends=True)
    desired = textwrap.dedent(replacement).strip()
    current = "".join(lines[node.lineno - 1 : node.end_lineno]).strip()
    if current == desired:
        return source
    block = desired + "\n\n"
    lines[node.lineno - 1 : node.end_lineno] = [block]
    return "".join(lines)


def insert_before_function_once(source: str, function_name: str, marker: str, block: str) -> str:
    if marker in source:
        return source
    node = top_level_node(source, function_name, (ast.FunctionDef, ast.AsyncFunctionDef))
    if node is None:
        raise RuntimeError(f"无法在 {function_name} 前插入补丁块")
    lines = source.splitlines(keepends=True)
    payload = textwrap.dedent(block).strip() + "\n\n"
    lines[node.lineno - 1 : node.lineno - 1] = [payload]
    return "".join(lines)


def append_once(source: str, marker: str, block: str) -> str:
    if marker in source:
        return source
    return source.rstrip() + "\n\n" + textwrap.dedent(block).strip() + "\n"


def replace_exact(source: str, old: str, new: str, label: str) -> str:
    if new in source:
        return source
    if old not in source:
        raise RuntimeError(f"未找到预期代码片段：{label}")
    return source.replace(old, new, 1)


PDF_PAGE_CLASS = r'''
@dataclass
class ParsedPage:
    page_num: int
    width: float
    height: float
    text: str
    spans: List[TextSpan] = field(default_factory=list)
    tables: List[Any] = field(default_factory=list)
    # Small inline checkbox images detected by PyMuPDF. Each entry contains
    # bbox=[x0,y0,x1,y1], checked=bool, and darkness_ratio=float.
    checkbox_marks: List[Dict[str, Any]] = field(default_factory=list)
'''

PDF_PARSE_FUNCTION = r'''
def _extract_checkbox_marks_fitz(doc: Any, page: Any) -> List[Dict[str, Any]]:
    """Detect tiny checked/unchecked bitmap boxes without OCR.

    ABB templates store checkbox glyphs as 19x19 inline images. The border of an
    unchecked box is dark, so classification uses only the inner image area.
    """
    marks: List[Dict[str, Any]] = []
    seen = set()
    try:
        import fitz

        for image in page.get_images(full=True) or []:
            xref = int(image[0])
            if xref in seen:
                continue
            seen.add(xref)
            pix = fitz.Pixmap(doc, xref)
            if pix.alpha or pix.n < 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if not (8 <= pix.width <= 64 and 8 <= pix.height <= 64 and pix.n >= 3):
                continue
            samples = bytes(pix.samples)
            margin_x = max(2, pix.width // 6)
            margin_y = max(2, pix.height // 6)
            dark = 0
            total = 0
            for y in range(margin_y, pix.height - margin_y):
                for x in range(margin_x, pix.width - margin_x):
                    offset = (y * pix.width + x) * pix.n
                    rgb = samples[offset : offset + 3]
                    if len(rgb) < 3:
                        continue
                    total += 1
                    if sum(rgb) / 3.0 < 180:
                        dark += 1
            ratio = dark / total if total else 0.0
            checked = ratio >= 0.03
            for rect in page.get_image_rects(xref) or []:
                if not (5 <= rect.width <= 24 and 5 <= rect.height <= 24):
                    continue
                marks.append({
                    "bbox": [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)],
                    "checked": checked,
                    "darkness_ratio": round(ratio, 4),
                })
    except Exception:
        return []
    return marks


def parse_pdf_with_evidence(filepath: str) -> ParsedPDF:
    """Read a PDF and extract text, coordinates, tables, and checkbox state."""
    try:
        import fitz  # PyMuPDF

        pages: List[ParsedPage] = []
        with fitz.open(filepath) as doc:
            for page_index, page in enumerate(doc, start=1):
                words = page.get_text("words", sort=True) or []
                spans = [
                    TextSpan(
                        text=str(word[4]),
                        bbox=(float(word[0]), float(word[1]), float(word[2]), float(word[3])),
                        block_no=int(word[5]) if len(word) > 5 else 0,
                        line_no=int(word[6]) if len(word) > 6 else 0,
                        word_no=int(word[7]) if len(word) > 7 else 0,
                    )
                    for word in words
                    if len(word) >= 5 and str(word[4]).strip()
                ]
                tables: List[Any] = []
                try:
                    finder = page.find_tables()
                    for table in getattr(finder, "tables", []) or []:
                        tables.append([[str(cell or "") for cell in row] for row in table.extract()])
                except Exception:
                    pass
                pages.append(
                    ParsedPage(
                        page_num=page_index,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        text=page.get_text("text", sort=True) or "",
                        spans=spans,
                        tables=tables,
                        checkbox_marks=_extract_checkbox_marks_fitz(doc, page),
                    )
                )
        result = ParsedPDF(filepath=filepath, pages=pages, full_text="\n".join(p.text for p in pages))
        if result.full_text.strip():
            return result
    except Exception:
        pass

    return _parse_with_pdfplumber(filepath)
'''

KNOWLEDGE_PARSE = r'''
def _exclusive_family_from_heading(value: str) -> str:
    """Return only mutually-exclusive option families.

    Broad sections such as “功能项” contain independent features and must never
    cause 3151-1 Program Package to conflict with 3107-1 Collision Detection.
    """
    key = _normalize_text(re.sub(r"^\d+(?:[.]\d+)*\s*", "", value or ""))
    for token, family in (
        ("颜色", "颜色"),
        ("防护等级", "防护等级"),
        ("控制器", "控制器"),
        ("示教器", "示教器"),
        ("本体电缆", "本体电缆"),
        ("io", "I/O"),
        ("质保", "质保"),
    ):
        if token in key:
            return family
    return ""


def _parse_mapping_markdown(text: str) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    aliases: Dict[str, Set[str]] = {}
    families: Dict[str, str] = {}
    current_family = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{2,6}\s+(.*)$", line)
        if heading:
            current_family = _exclusive_family_from_heading(heading.group(1))
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        code_match = re.fullmatch(r"\d{3,4}-\d{1,4}", cells[0].replace(" ", ""))
        if not code_match:
            continue
        code = code_match.group(0)
        bucket = aliases.setdefault(code, set())
        for cell in cells[1:]:
            bucket.update(_split_alias_cell(cell))
        if current_family:
            families[code] = current_family
    return aliases, families
'''

TA_START = r'''
def _find_ta_start(parsed: ParsedPDF) -> Optional[int]:
    """Find an embedded TA cover, not a table-of-contents mention."""
    for page in parsed.pages:
        key = match_key(page.text)
        if (
            "technicalagreement" in key
            or "技术协议书" in key
            or "docno.3.02.f03" in key
            or "docno3.02.f03" in key
        ):
            return page.page_num
    return None
'''

TA_END_HELPER = r'''
# PM_TA_RANGE_PATCH_20260716
def _find_ta_end(parsed: ParsedPDF, start_page: int) -> int:
    """Return the first page after an embedded TA.

    Prefer the TA's own “Page 1 of N” pagination. Fall back to the first integrity
    appendix page outside Doc No. 3.02.F03. This prevents integrity pages from
    being counted as TA pages or contaminating model/configuration extraction.
    """
    max_page = max((page.page_num for page in parsed.pages), default=start_page)
    cover = next((page for page in parsed.pages if page.page_num == start_page), None)
    if cover:
        match = re.search(r"Page\s*1\s*of\s*(\d{1,3})", cover.text or "", re.I)
        if match:
            expected = int(match.group(1))
            if 1 <= expected <= 100:
                return min(max_page + 1, start_page + expected)
    for page in parsed.pages:
        if page.page_num <= start_page:
            continue
        key = match_key(page.text)
        if "诚信条款" in page.text and "docno3.02.f03" not in key:
            return page.page_num
    return max_page + 1
'''

RESOLVE_DOCUMENTS = r'''
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

    for parsed in parsed_by_path.values():
        text_key = match_key(parsed.full_text)
        compact = re.sub(r"\s+", "", parsed.full_text or "")
        if contract is None and "销售合同" in parsed.full_text and re.search(r"[MK]\d{4}-\d{4}", compact, re.I):
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
            ta_end = _find_ta_end(contract, ta_start)
            contract_body = _slice_pdf(contract, (page for page in contract.pages if page.page_num < ta_start), "#contract")
            if ta is None:
                ta_pages = [page for page in contract.pages if ta_start <= page.page_num < ta_end]
                if ta_pages:
                    ta = _slice_pdf(contract, ta_pages, "#ta")
                    ta_embedded = True
    return DocumentSet(contract, contract_body, cqp, ta, ta_embedded)
'''

PAYMENT_SOURCE = r'''
def _extract_payment_source(text: str) -> str:
    """Prefer the real Annex 2 payment section over TOC/earlier mentions."""
    source = _normalize_extraction_text(text).replace("\r\n", "\n").replace("\r", "\n")
    annexes = list(re.finditer(r"附件\s*二[^\n]{0,120}(?:付款方式|付款条件)", source, re.I))
    if annexes:
        start = annexes[-1].start()
    else:
        headings = list(re.finditer(r"(?m)^\s*(?:付款条件|付款方式)\s*[:：]?", source, re.I))
        if headings:
            start = headings[-1].start()
        else:
            inline = list(re.finditer(r"(?:付款条件|付款方式)\s*[:：]?", source, re.I))
            start = inline[-1].start() if inline else 0
    section = source[start:]
    end = re.search(
        r"(?m)^\s*(?:附件\s*[三四五六]|诚信条款|违约责任|合同解除|解除合同|取消条款|"
        r"质量保证|质保条款|保修条款|交货时间|交期|交货条款|贸易术语|签字盖章)\b",
        section[1:],
        re.I,
    )
    if end:
        section = section[: end.start() + 1]
    return section[:8000].strip()
'''

LOOKS_CONFIG = r'''
def _looks_like_configuration_code(code: str, line: str, description: str) -> bool:
    """Reject dates, contract/model fragments, and physical ranges."""
    try:
        prefix_text, suffix_text = code.split("-", 1)
        prefix, suffix = int(prefix_text), int(suffix_text)
    except (TypeError, ValueError):
        return False

    normalized_line = _normalize_extraction_text(line)
    context = f"{normalized_line} {description}"
    if 1900 <= prefix <= 2099:
        return False
    if re.search(rf"(?:IRB\s*)?{prefix}\s*-\s*{suffix}\s*/\s*\d", context, re.I):
        return False
    if re.search(rf"[A-Za-z]\s*{re.escape(code)}", normalized_line):
        return False
    if re.search(r"(?:日期|date|合同编号|contract\s*no)\s*[:：]?\s*" + re.escape(code), context, re.I):
        return False
    physical_words = (
        r"电压|伏特|频率|温度|重量|负载|工作范围|行程|速度|精度|半径|长度|宽度|高度|"
        r"voltage|frequency|temperature|weight|payload|reach|range|speed"
    )
    physical_units = r"V|伏|Hz|kW|W|A|mA|mm|cm|kg|N|Nm|m/s|℃|°C"
    if re.search(rf"(?:{physical_words})[^\n]{{0,35}}{re.escape(code)}", context, re.I):
        return False
    if re.search(rf"{re.escape(code)}\s*(?:{physical_units})\b", context, re.I):
        return False
    if 100 <= prefix <= 1000 and 100 <= suffix <= 1000 and re.search(physical_units, context, re.I):
        return False
    return True
'''

QUANTITY_HELPER = r'''
# PM_QUANTITY_EVIDENCE_PATCH_20260716
def _extract_model_quantity_details(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    """Return selected quantities plus explicit within-document contradictions."""
    if parsed is None:
        return {"selected": {}, "candidates": {}, "conflicts": []}
    candidates: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    spec_words = re.compile(
        r"负载|工作范围|重复定位|电压|频率|重量|速度|轴数|防护等级|payload|reach|range|"
        r"repeatability|voltage|frequency|weight|speed",
        re.I,
    )
    spec_unit = re.compile(r"^\s*(?:kg|g|mm|cm|m\b|N\b|Nm|kW|W\b|V\b|Hz|℃|°C|轴|级)", re.I)

    def add(model: str, score: int, qty: int, page: int, line: str) -> None:
        if 0 < qty < 1000:
            candidates[model].append({"score": score, "qty": qty, "page": page, "line": _clean_inline(line)})

    for page in parsed.pages:
        page_text = _normalize_extraction_text(page.text)
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        page_is_table = bool(re.search(r"供货范围|设备清单|机器人清单|产品清单|数量|qty|quantity", page_text, re.I))
        for index, line in enumerate(lines):
            for model_match in GENERIC_MODEL_PATTERN.finditer(line):
                model = _canonical_model(model_match.group(0))
                before, after = line[: model_match.start()], line[model_match.end() :]
                before_match = re.search(r"(\d{1,4})\s*(?:台|套|pcs?|units?)\s*$", before, re.I)
                if before_match:
                    add(model, 100, int(before_match.group(1)), page.page_num, line)
                labelled = re.search(r"(?:数量|qty|quantity)\s*[:：]?\s*(\d{1,4})(?:\s*(?:台|套|pcs?|units?))?", after[:160], re.I)
                if labelled:
                    add(model, 100, int(labelled.group(1)), page.page_num, line)
                unit_after = re.search(r"(?:^|[|｜:：,，;；])\s*(\d{1,4})\s*(?:台|套|pcs?|units?)\b", after[:160], re.I)
                if unit_after:
                    add(model, 98, int(unit_after.group(1)), page.page_num, line)
                for nearby in lines[index + 1 : index + 4]:
                    nearby_qty = re.fullmatch(r"\s*(\d{1,4})\s*(?:台|套|pcs?|units?)\s*", nearby, re.I)
                    if nearby_qty:
                        add(model, 96, int(nearby_qty.group(1)), page.page_num, line + " | " + nearby)
                        break
                    if GENERIC_MODEL_PATTERN.search(nearby):
                        break
                if page_is_table and not spec_words.search(line):
                    table_qty = re.match(r"\s*(?:[|｜:：,，;；-]\s*)?(\d{1,4})(?![\d./-])", after)
                    if table_qty and not spec_unit.match(after[table_qty.end() :]):
                        add(model, 70, int(table_qty.group(1)), page.page_num, line)

    selected: Dict[str, int] = {}
    conflicts: List[Dict[str, Any]] = []
    for model, values in candidates.items():
        top_score = max(item["score"] for item in values)
        top_values = [item["qty"] for item in values if item["score"] == top_score]
        counts = {value: top_values.count(value) for value in set(top_values)}
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if ranked:
            selected[model] = ranked[0][0]
        explicit_values = sorted({item["qty"] for item in values if item["score"] >= 96})
        if len(explicit_values) > 1:
            conflicts.append({
                "model": model,
                "values": explicit_values,
                "selected": selected.get(model, 0),
                "evidence": [item for item in values if item["score"] >= 96],
            })
    return {"selected": selected, "candidates": dict(candidates), "conflicts": conflicts}
'''

QUANTITY_WRAPPER = r'''
def _extract_model_quantities(parsed: Optional[ParsedPDF]) -> Dict[str, int]:
    return dict(_extract_model_quantity_details(parsed).get("selected", {}))
'''

PAYMENT_TERMS = r'''
def _extract_payment_terms(text: str) -> Dict[str, Any]:
    section_raw = _extract_payment_source(text)
    section = _normalize_extraction_text(section_raw)
    chinese_digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

    def chinese_percent(token: str) -> Optional[float]:
        if token == "一百":
            return 100.0
        if "十" in token:
            left, _, right = token.partition("十")
            return float((chinese_digits.get(left, 1) if left else 1) * 10 + (chinese_digits.get(right, 0) if right else 0))
        return float(chinese_digits[token]) if token in chinese_digits else None

    def percentages(clause: str) -> List[float]:
        arabic = [float(value) for value in re.findall(r"(\d+(?:[.]\d+)?)\s*%", clause)]
        if arabic:
            return arabic
        output: List[float] = []
        for token in re.findall(r"百分之([零一二三四五六七八九十百]+)", clause):
            value = chinese_percent(token)
            if value is not None:
                output.append(value)
        return output

    marker_pattern = re.compile(r"(?:^|[\n;；])\s*(?:\d+|[一二三四五六七八九十]+)\s*[）).、]", re.M)
    starts = [0]
    for match in marker_pattern.finditer(section):
        start = match.start()
        if start < len(section) and section[start] in "\n;；":
            start += 1
        if start > starts[-1]:
            starts.append(start)
    starts = sorted(set(starts))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(section)
        clause = _clean_inline(section[start:end])
        if clause:
            clauses.append(clause)
    if len(clauses) <= 1:
        percent_starts = [match.start() for match in re.finditer(r"(?:合同总价的)?(?:\d+(?:[.]\d+)?\s*%|百分之[零一二三四五六七八九十百]+)", section)]
        if len(percent_starts) > 1:
            starts = [0] + percent_starts[1:]
            clauses = [_clean_inline(section[start:(starts[i + 1] if i + 1 < len(starts) else len(section))]) for i, start in enumerate(starts)]

    payment_hints = ("合同总价", "合同价款", "货款", "预付款", "付款", "支付", "电汇", "承兑", "发货", "交付", "验收", "开票")
    exclusions = ("违约金", "罚金", "罚款", "增值税率", "税率", "利息", "取消费", "赔偿")
    values: List[float] = []
    installments: List[Dict[str, Any]] = []
    for clause in clauses:
        found = percentages(clause)
        if not found or not any(hint in clause for hint in payment_hints):
            continue
        if any(word in clause for word in exclusions) and not any(word in clause for word in ("预付款", "付款", "货款", "合同总价")):
            continue
        values.extend(found)
        trigger = _match_first(clause, [
            r"((?:签订(?:本)?合同|合同生效|预付款(?:到账)?|设备发货|发货|交付|验收)[^，。；;]{0,60}?(?:前|后|时|内))"
        ])
        for percent in found:
            installments.append({
                "percent": int(percent) if percent.is_integer() else percent,
                "text": clause,
                "trigger": trigger,
                "method": "银行承兑汇票" if "承兑" in clause else ("电汇" if "电汇" in clause else ""),
            })

    total = sum(values)
    if not section_raw:
        extraction_state = "MISSING"
    elif not values:
        extraction_state = "EXTRACTION_FAILED"
    elif abs(total - 100) >= 0.01:
        extraction_state = "INCOMPLETE"
    else:
        extraction_state = "EXTRACTED"
    return {
        "installments": installments,
        "percentages": values,
        "raw": section_raw,
        "bank": _match_first(section, [r"用户银行[:：]\s*([^\n]+)", r"(?:结算银行|开户银行)[:：]?\s*([^\n]+)"]),
        "account_name": _match_first(section, [r"(?:账户名称|户名|名称)[:：]\s*([^\n]+)"]),
        "account_number": re.sub(r"\s+", "", _match_first(section, [r"(?:账户号码|帐号|账号)[:：]\s*([\d\s-]+)"])),
        "complete": bool(section_raw and values and abs(total - 100) < 0.01),
        "extraction_state": extraction_state,
    }
'''

CONFIG_HELPER = r'''
# PM_CONFIG_SECTION_PATCH_20260716
def _is_model_section_line(line: str, model_matches: Sequence[re.Match[str]], current_model: str = "") -> bool:
    if len(model_matches) != 1:
        return False
    match = model_matches[0]
    prefix = line[: match.start()].strip()
    suffix = line[match.end() :].strip()
    if not prefix and (not suffix or re.search(r"Industrial\s+robot|Industry\s+Robot|型(?:单臂)?工业机器人", suffix, re.I)):
        return True
    if re.fullmatch(r"(?:\d+[.)、]?\s*)?", prefix) and re.search(r"Industrial\s+robot|Industry\s+Robot|型(?:单臂)?工业机器人|system", suffix, re.I):
        return True
    if re.match(r"^\s*\d+[.)、]?\s*IRB\b", line, re.I):
        return True
    return not current_model and bool(re.match(r"^\s*IRB\b", line, re.I))
'''

CONFIG_EXTRACT = r'''
def _extract_configurations(parsed: Optional[ParsedPDF], source_type: str) -> List[Dict[str, Any]]:
    """Extract model-bound options with cross-page section continuity."""
    if parsed is None:
        return []
    code_pattern = re.compile(r"(?<![A-Za-z0-9.])(\d{3,4}-\d{1,4})(?![\d.])")
    configs: List[Dict[str, Any]] = []
    current_model = ""
    for page in parsed.pages:
        lines = [_normalize_extraction_text(line).strip() for line in page.text.splitlines() if _normalize_extraction_text(line).strip()]
        for index, line in enumerate(lines):
            model_matches = list(GENERIC_MODEL_PATTERN.finditer(line))
            if _is_model_section_line(line, model_matches, current_model):
                current_model = _canonical_model(model_matches[0].group(0))
            for match in code_pattern.finditer(line):
                if any(model.start() <= match.start() < model.end() for model in model_matches):
                    continue
                code_value = match.group(1)
                description = line[match.end() :].strip(" :：-—|｜")
                if not description and index + 1 < len(lines):
                    next_line = lines[index + 1]
                    if not code_pattern.search(next_line) and not GENERIC_MODEL_PATTERN.search(next_line):
                        description = next_line
                if not _looks_like_configuration_code(code_value, line, description):
                    continue
                if code_value.startswith("3300-"):
                    detected = _canonical_model(description)
                    if detected.startswith("IRB "):
                        current_model = detected
                if not current_model:
                    continue
                configs.append({
                    "model": current_model,
                    "code": code_value,
                    "description": _clean_inline(description),
                    "page": page.page_num,
                    "source": source_type,
                })

    unique: List[Dict[str, Any]] = []
    seen = set()
    for config in configs:
        key = (config.get("model"), config.get("code"), normalize_alias_text(config.get("description", "")))
        if key not in seen:
            unique.append(config)
            seen.add(key)
    return unique
'''

MODEL_SECTION = r'''
def _model_section_text(parsed: Optional[ParsedPDF], model: str) -> str:
    """Return a model section while retaining continuation rows on later pages."""
    if parsed is None:
        return ""
    target = _canonical_model(model)
    current_model = ""
    sections: Dict[str, List[str]] = defaultdict(list)
    for page in parsed.pages:
        for raw_line in page.text.splitlines():
            line = _normalize_extraction_text(raw_line).strip()
            if not line:
                continue
            matches = list(GENERIC_MODEL_PATTERN.finditer(line))
            if _is_model_section_line(line, matches, current_model):
                current_model = _canonical_model(matches[0].group(0))
            if current_model:
                sections[current_model].append(line)
    return "\n".join(sections.get(target, []))[:20000]
'''

WARRANTY_HELPER = r'''
# PM_WARRANTY_PROSE_PATCH_20260716
def _warranty_details_from_document(parsed: Optional[ParsedPDF], configs: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    result = _warranty_config_details(configs)
    models = {str(item.get("model", "")) for item in configs if item.get("model")}
    if parsed:
        models.update(_extract_model_quantities(parsed))
        models.update(_models_in_text(parsed.full_text))
    for model in models:
        section = _model_section_text(parsed, model)
        classification = _warranty_class_from_text(section)
        if classification == "Unknown":
            continue
        current = result.get(model, {})
        result[model] = {
            "code": current.get("code", ""),
            "description": current.get("description", "") or classification,
            "classification": classification,
        }
    return result
'''

INCOTERM_DETECT = r'''
def _detect_contract_incoterm(source: Any) -> Dict[str, Any]:
    """Read DDP/EXW from text markers or nearby inline checkbox images."""
    parsed = source if isinstance(source, ParsedPDF) else None
    pages = parsed.pages[:5] if parsed else []
    text = "\n".join(page.text for page in pages) if parsed else str(source or "")
    lines: List[Dict[str, str]] = []
    selected: List[str] = []

    def visual_state(page: ParsedPage, keyword: str) -> str:
        spans = [span for span in page.spans if keyword in _clean_inline(span.text)]
        if not spans:
            return "unknown"
        span = spans[0]
        sy = (span.bbox[1] + span.bbox[3]) / 2.0
        candidates = []
        for mark in getattr(page, "checkbox_marks", []) or []:
            bbox = mark.get("bbox") or []
            if len(bbox) != 4:
                continue
            mx = float(bbox[2])
            my = (float(bbox[1]) + float(bbox[3])) / 2.0
            if mx <= span.bbox[0] + 5 and abs(my - sy) <= 9:
                candidates.append((abs(my - sy), float(span.bbox[0]) - mx, mark))
        if not candidates:
            return "unknown"
        mark = min(candidates, key=lambda item: (item[0], item[1]))[2]
        return "checked" if bool(mark.get("checked")) else "unchecked"

    if parsed:
        for page in pages:
            for raw_line in unicodedata.normalize("NFKC", page.text or "").splitlines():
                line = _clean_inline(raw_line)
                if "到货价" not in line and "出厂价" not in line:
                    continue
                term = "DDP" if "到货价" in line else "EXW"
                keyword = "到货价" if term == "DDP" else "出厂价"
                state = visual_state(page, keyword)
                if state == "unknown":
                    checked = any(marker in line for marker in CHECKED_MARKERS)
                    unchecked = any(marker in line for marker in UNCHECKED_MARKERS)
                    state = "checked" if checked and not unchecked else ("unchecked" if unchecked and not checked else "unknown")
                lines.append({"term": term, "state": state, "text": line})
                if state == "checked":
                    selected.append(term)
    else:
        for raw_line in unicodedata.normalize("NFKC", text).splitlines():
            line = _clean_inline(raw_line)
            if "到货价" not in line and "出厂价" not in line:
                continue
            term = "DDP" if "到货价" in line else "EXW"
            checked = any(marker in line for marker in CHECKED_MARKERS)
            unchecked = any(marker in line for marker in UNCHECKED_MARKERS)
            state = "checked" if checked and not unchecked else ("unchecked" if unchecked and not checked else "unknown")
            lines.append({"term": term, "state": state, "text": line})
            if state == "checked":
                selected.append(term)
    selected_terms = sorted(set(selected))
    return {
        "selected": selected_terms[0] if len(selected_terms) == 1 else "",
        "selected_terms": selected_terms,
        "conflict": len(selected_terms) > 1,
        "lines": lines,
        "candidates": sorted(set(item["term"] for item in lines)),
    }
'''

EXTRACTION_HELPERS = r'''
# PM_EXTRACTION_HELPERS_PATCH_20260716
def _extract_business_number(text: str) -> str:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    match = re.search(r"\b([MK]\d{4}-\d{4})\b", compact, re.I)
    return match.group(1).upper() if match else ""


def _multiline_label_value(text: str, label: str, max_lines: int = 4) -> str:
    stop_labels = (
        "报价编号", "报价单编号", "报价修订版本", "询价日期", "报价日期", "负责人",
        "客户", "联络人", "联络地址", "地址 / 街道", "地址/街道", "邮政编码", "城市",
        "国家或地区", "页码", "目录",
    )
    lines = (text or "").splitlines()
    for index, raw_line in enumerate(lines):
        clean = _clean_inline(raw_line)
        pos = clean.find(label)
        if pos < 0:
            continue
        first = clean[pos + len(label) :].lstrip(" :：")
        parts = [first] if first else []
        for following in lines[index + 1 : index + max_lines]:
            value = _clean_inline(following)
            if not value:
                continue
            if any(value.startswith(stop) or stop in value[:18] for stop in stop_labels):
                break
            if re.match(r"^\d+[./]\d+", value):
                break
            parts.append(value)
        if parts:
            return "".join(parts)
    return ""


def _clean_payment_verbatim(value: str) -> str:
    lines = []
    for raw in (value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if re.search(r"^(?:\d+\s*/\s*\d+|Doc\s+No\.|ABB\s+Robotics\s+China|Rev\s+No\.)", line, re.I):
            continue
        line = re.sub(r"[\uf000-\uf8ff]", "", line).strip()
        if line:
            lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
'''

EXTRACT_CONTRACT = r'''
def extract_contract(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = _normalize_extraction_text(parsed.full_text)
    first_pages = "\n".join(_normalize_extraction_text(page.text) for page in parsed.pages[:3])
    transport_text = "\n".join(_normalize_extraction_text(page.text) for page in parsed.pages[:5])
    signature_text = "\n".join(_normalize_extraction_text(page.text) for page in parsed.pages[-3:])
    contract_number = _extract_business_number(text)
    buyer = _match_first(first_pages, [r"买方[:：]\s*(.*?)\s*地址[:：]", r"甲方[（(]买方[）)][:：]\s*([^\n]+)"], re.S)
    seller = _match_first(first_pages, [r"卖方[:：]\s*(.*?)\s*地址[:：]", r"乙方[（(]卖方[）)][:：]\s*([^\n]+)"], re.S)
    buyer_address = _match_first(first_pages, [r"买方[:：].*?地址[:：]\s*(.*?)\s*卖方[:：]", r"买方地址[:：]\s*([^\n]+)"], re.S)
    seller_address = _match_first(first_pages, [r"卖方[:：].*?地址[:：]\s*(.*?)(?:目录|合同编号|$)", r"卖方地址[:：]\s*([^\n]+)"], re.S)
    quantity_details = _extract_model_quantity_details(parsed)
    quantities = quantity_details["selected"]
    delivery_schedule = _extract_delivery_schedule(parsed)
    money = r"([\d,]+(?:[.]\d{1,2})?)"
    untaxed_text = _match_first(text, [rf"不含增值税(?:总额|金额)?(?:为)?\s*[:：]?\s*(?:CNY|RMB|人民币)?\s*[:：]?\s*{money}", rf"(?:未税金额|未税总额)\s*[:：]?\s*(?:CNY|RMB|人民币)?\s*[:：]?\s*{money}"])
    gross_text = _match_first(text, [rf"(?<!不)含增值税(?:总额|金额)?(?:为)?\s*[:：]?\s*(?:CNY|RMB|人民币)?\s*[:：]?\s*{money}", rf"合同价格的含增值税总额为\s*[:：]?.*?{money}"])
    vat_text = _match_first(text, [r"(?:增值税(?:税率)?|税率)\s*[:：]?\s*(\d{1,2}(?:[.]\d+)?)\s*%"])
    delivery_trigger = ""
    for phrase in ("合同生效且收到预付款后", "合同生效并收到预付款后", "合同生效且预付款到账后", "预付款到账后"):
        if phrase in text:
            delivery_trigger = phrase
            break
    placeholder_text = re.sub(r"\s+", "", signature_text + "\n" + text)
    known_placeholders = {"@@@Chop_ABB", "@@@Chop_Customer", "@@@Sign_ABBPerson", "@@@Sign_CustomerPerson"}
    payment_terms = _extract_payment_terms(text)
    return {
        "contract_number": contract_number,
        "cqp_reference": _match_first(text, [r"(?:单价信息|报价单|CQP)[:：]?\s*(CQ\d{7})", r"\b(CQ\d{7})\b"]),
        "buyer_name": _clean_entity(buyer),
        "buyer_address": _clean_entity(buyer_address),
        "seller_name": _clean_entity(seller),
        "seller_address": _clean_entity(seller_address),
        "project_name": _match_first(text, [r"买方基于(.{2,80}?)项目需求", r"项目名称[:：]\s*([^\n]+)"]),
        "end_customer_name": _clean_entity(_match_first(text, [r"(?:最终用户|终端客户)(?:名称)?[:：]\s*([^\n]+)"])),
        "end_customer_address": _clean_entity(_match_first(text, [r"(?:最终用户|终端客户)地址[:：]\s*([^\n]+)"])),
        "installation_location": _clean_entity(_match_first(text, [r"(?:设备)?安装地点[:：]\s*([^\n]+)"])),
        "delivery_location": _clean_entity(_match_first(text, [r"(?:交付|交货)地点[:：]\s*([^\n]+)"])),
        "ship_to_name": _clean_entity(_match_first(text, [r"(?:Ship[- ]?to|收货方)(?:名称)?[:：]\s*([^\n]+)"])),
        "ship_to_address": _clean_entity(_match_first(text, [r"(?:Ship[- ]?to|收货方)地址[:：]\s*([^\n]+)"])),
        "seller_origin": _clean_inline(_match_first(transport_text, [r"从([^\n，。]{2,60}?)发出", r"在([^\n，。]{2,60}?)工厂内包装完毕"])),
        "sales_person": _clean_inline(_match_first(text, [r"(?:销售人员|销售负责人|Sales)\s*[:：]?\s*([^\n]+)"])),
        "pm": _clean_inline(_match_first(text, [r"(?:项目经理|PM)\s*[:：]?\s*([^\n]+)"])),
        "products": [{"model": model, "qty": qty} for model, qty in quantities.items()],
        "total_qty": sum(quantities.values()),
        "quantity_conflicts": quantity_details.get("conflicts", []),
        "delivery_schedule": delivery_schedule,
        "delivery_trigger": delivery_trigger,
        "split_delivery": bool(re.search(r"允许分批(?:装运|发货)", text)),
        "incoterm_detection": _detect_contract_incoterm(parsed),
        "untaxed_amount": _parse_money(untaxed_text),
        "vat_rate": float(vat_text) / 100 if vat_text else 0.0,
        "tax_included_amount": _parse_money(gross_text),
        "payment_terms": payment_terms,
        "warranty": _extract_warranty_clause(text),
        "signature_placeholders": sorted(token for token in known_placeholders if token in placeholder_text),
        "blank_signature_dates": bool(re.search(r"日期[:：]\s+日期[:：]", signature_text) or re.search(r"日期[:：]\s*(?:\n|$)", signature_text)),
        "attachments": {"ta": bool(re.search(r"附件一[^\n]{0,80}技术协议", text)), "payment": bool(re.search(r"附件二[^\n]{0,80}(?:付款方式|付款条件)", text)), "integrity": bool(re.search(r"附件三[^\n]{0,80}诚信条款", text))},
        "file_priority": "本合同优先于附件" if re.search(r"本合同.*?优先于.*?附件|文件优先性", text, re.S) else "",
        "extraction_state": {"products": "EXTRACTED" if quantities else "EXTRACTION_FAILED", "delivery_schedule": "EXTRACTED" if delivery_schedule else "EXTRACTION_FAILED", "untaxed_amount": "EXTRACTED" if untaxed_text else "EXTRACTION_FAILED", "vat_rate": "EXTRACTED" if vat_text else "EXTRACTION_FAILED", "tax_included_amount": "EXTRACTED" if gross_text else "EXTRACTION_FAILED", "payment_terms": payment_terms.get("extraction_state", "EXTRACTION_FAILED")},
    }
'''

EXTRACT_CQP = r'''
def extract_cqp(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = _normalize_extraction_text(parsed.full_text)
    products = _extract_cqp_products(parsed)
    for product in products:
        expected = round(float(product.get("qty", 0)) * float(product.get("unit_price", 0)), 2)
        product["expected_line_total"] = expected
        product["line_total_difference"] = round(float(product.get("line_total", 0)) - expected, 2)
    configs = _extract_configurations(parsed, "cqp")
    warranty_details = _warranty_details_from_document(parsed, configs)
    number = re.search(r"(?:报价单编号|报价编号)[:：]?\s*(CQ\d{7})", text) or re.search(r"\b(CQ\d{7})\b", text)
    customer = _first_line_value(text, "客户") or _match_first(text, [r"客户(?:名称)?[:：]\s*([^\n]+)"])
    customer_address = _first_line_value(text, "联络地址") or _match_first(text, [r"客户地址[:：]\s*([^\n]+)"])
    seller = _match_first(text, [r"ABB\s*公司名称[:：]?\s*([^\n]+)", r"ABB\s*单位名称[:：]?\s*([^\n]+)"])
    money_values = [_parse_money(value) for value in re.findall(r"CNY\s*[:：]?\s*([\d,]+(?:[.]\d{1,2})?)", text, re.I)]
    untaxed = _parse_money(_match_first(text, [r"(?:未税|不含税)(?:总额|金额)?[:：]?\s*(?:CNY|RMB)?\s*[:：]?\s*([\d,]+(?:[.]\d{1,2})?)"]))
    tax_amount = _parse_money(_match_first(text, [r"(?:增值税额|税额)[:：]?\s*(?:CNY|RMB)?\s*[:：]?\s*([\d,]+(?:[.]\d{1,2})?)"]))
    gross = _parse_money(_match_first(text, [r"(?:含税总额|含税金额)[:：]?\s*(?:CNY|RMB)?\s*[:：]?\s*([\d,]+(?:[.]\d{1,2})?)"]))
    if not untaxed and money_values:
        untaxed = money_values[0]
    if not tax_amount and len(money_values) >= 2:
        tax_amount = money_values[1]
    if not gross and len(money_values) >= 3:
        gross = money_values[2]
    delivery_time = _match_first(text, [r"交货时间[:：]?\s*([^\n]+)", r"交期[:：]?\s*([^\n]+)"])
    delivery_term = _match_first(text, [r"交货条款[:：]?\s*([^\n]+)", r"贸易术语[:：]?\s*([^\n]+)"])
    delivery_schedule = _extract_delivery_schedule(parsed)
    weeks = re.search(r"(\d+)\s*周", delivery_time)
    vat = re.search(r"增值税(?:率)?\s*[:：]?\s*(\d{1,2}(?:[.]\d+)?)\s*%", text)
    payment_terms = _extract_payment_terms(text)
    return {
        "cqp_number": number.group(1) if number else "",
        "version": _match_first(text, [r"报价修订版本[:：]?\s*([A-Za-z0-9._-]+)", r"版本号[:：]?\s*([A-Za-z0-9._-]+)"]),
        "project_name": _clean_inline(_multiline_label_value(text, "项目名称") or _first_line_value(text, "项目名称") or _match_first(text, [r"项目名称[:：]?\s*([^\n]+)"])),
        "customer_name": _clean_inline(customer),
        "customer_address": _clean_inline(customer_address),
        "customer_postal_code": _clean_inline(_first_line_value(text, "邮政编码")),
        "end_user": _clean_entity(_match_first(text, [r"(?:最终用户|终端客户)(?:名称)?[:：]\s*([^\n]+)"])),
        "end_user_address": _clean_entity(_match_first(text, [r"(?:最终用户|终端客户)地址[:：]\s*([^\n]+)"])),
        "ship_to_name": _clean_entity(_match_first(text, [r"(?:Ship[- ]?to|收货方)(?:名称)?[:：]\s*([^\n]+)"])),
        "ship_to_address": _clean_entity(_match_first(text, [r"(?:Ship[- ]?to|收货方)地址[:：]\s*([^\n]+)"])),
        "seller_name": _clean_entity(seller),
        "sales_person": _clean_inline(_match_first(text, [r"(?:负责人|销售人员|Sales)\s*[:：]?\s*([^\n]+)"])),
        "products": products,
        "total_qty": sum(int(product.get("qty", 0)) for product in products),
        "untaxed_total": untaxed,
        "tax_amount": tax_amount,
        "tax_included_total": gross,
        "vat_rate": float(vat.group(1)) / 100 if vat else 0.0,
        "payment_terms": payment_terms,
        "delivery_time": _clean_inline(delivery_time),
        "delivery_weeks": int(weeks.group(1)) if weeks else 0,
        "delivery_schedule": delivery_schedule,
        "delivery_trigger": "预付款到账" if "预付款" in delivery_time else ("合同生效" if "合同生效" in delivery_time else ""),
        "delivery_term": _clean_inline(delivery_term),
        "warranty_terms": _clean_inline(_match_first(text, [r"质量保证[:：]?\s*([^\n]+)", r"质保[:：]?\s*([^\n]+)"])),
        "configurations": configs,
        "warranty_codes_by_model": {model: data.get("code", "") for model, data in warranty_details.items() if data.get("code")},
        "warranty_details_by_model": warranty_details,
        "extraction_state": {"products": "EXTRACTED" if products else "EXTRACTION_FAILED", "configurations": "EXTRACTED" if configs else "EXTRACTION_FAILED", "payment_terms": payment_terms.get("extraction_state", "EXTRACTION_FAILED")},
    }
'''

TA_RED_FLAGS = r'''
# PM_TA_RED_FLAGS_PATCH_20260716
def _extract_ta_technical_red_flags(parsed: Optional[ParsedPDF]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if parsed is None:
        return issues
    for page in parsed.pages:
        text = _normalize_extraction_text(page.text)
        for match in re.finditer(r"位置重复精度\s*[:：]\s*([0-9]+(?:[.]\d+)?)\s*(mm|m)\b", text, re.I):
            value, unit = float(match.group(1)), match.group(2).lower()
            if unit == "m" and value < 0.1:
                issues.append({"type": "repeatability_unit", "page": page.page_num, "quote": _clean_inline(match.group(0)), "detail": f"位置重复精度写为{value:g}m（等于{value * 1000:g}mm），单位高度可疑，必须由技术人员确认。"})
        # Normalize one physical line at a time. Whole-page normalization turns
        # the newline after a trailing colon into a space, which would make the
        # next numbered heading look like the SafeMove field value.
        lines = [
            _clean_inline(_normalize_extraction_text(line))
            for line in (page.text or "").splitlines()
            if _clean_inline(_normalize_extraction_text(line))
        ]
        for index, line in enumerate(lines):
            match = re.search(r"Safe\s*move\s*功能\s*[:：]\s*(.*)$", line, re.I)
            if not match:
                continue
            value = match.group(1).strip(" _-—")
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            numbered_heading = bool(
                re.match(r"^\d+(?:[.]\d+)*(?:[.、）)]|\s)", next_line)
            )
            if not value and (not next_line or numbered_heading):
                issues.append({"type": "blank_safemove", "page": page.page_num, "quote": line, "detail": "TA中的SafeMove功能字段为空，但配置清单存在SafeMove相关项，需补全或确认。"})
    unique = []
    seen = set()
    for issue in issues:
        key = (issue["type"], issue["page"], issue["quote"])
        if key not in seen:
            unique.append(issue)
            seen.add(key)
    return unique
'''

EXTRACT_TA = r'''
def extract_ta(parsed: Optional[ParsedPDF]) -> Dict[str, Any]:
    if parsed is None:
        return {}
    text = _normalize_extraction_text(parsed.full_text)
    configs = _extract_configurations(parsed, "ta")
    quantity_details = _extract_model_quantity_details(parsed)
    quantities = quantity_details["selected"]
    compact_text = re.sub(r"\s+", "", text)
    placeholders = {"@@@Chop_ABB", "@@@Chop_Customer", "@@@Sign_ABBPerson", "@@@Sign_CustomerPerson"}
    warranty_details = _warranty_details_from_document(parsed, configs)
    quotation_number = _match_first(text, [r"Quotation\s*No[.]?\s*[:：]\s*([^\n]+)", r"报价编号\s*[:：]\s*(CQ\d{7})"])
    quotation_blank = bool(re.search(r"Quotation\s*No[.]?\s*[:：]\s*(?:\n|$)", text, re.I)) and not quotation_number
    return {
        "contract_number": _extract_business_number(text),
        "quotation_number": _clean_inline(quotation_number),
        "quotation_number_blank": quotation_blank,
        "buyer_name": _clean_entity(_match_first(text, [r"甲方[（(]买方[）)][:：]\s*([^\n]+)", r"买方[:：]\s*([^\n]+)"])),
        "buyer_address": _clean_entity(_match_first(text, [r"买方地址[:：]\s*([^\n]+)"])),
        "seller_name": _clean_entity(_match_first(text, [r"卖方[（(]乙方[）)][:：]\s*([^\n]+)", r"乙方[（(]卖方[）)][:：]\s*([^\n]+)"])),
        "products": [{"model": model, "qty": qty} for model, qty in quantities.items()],
        "total_qty": sum(quantities.values()),
        "quantity_conflicts": quantity_details.get("conflicts", []),
        "configurations": configs,
        "warranty_codes_by_model": {model: data.get("code", "") for model, data in warranty_details.items() if data.get("code")},
        "warranty_details_by_model": warranty_details,
        "technical_red_flags": _extract_ta_technical_red_flags(parsed),
        "lps_name_in_supply": "LPS" if re.search(r"IRB\s*\d{3,4}[^\n]{0,40}\bLPS\b", text, re.I) else "",
        "lps_name_in_parameters": "Lite+" if re.search(r"IRB\s*\d{3,4}[^\n]{0,40}Lite\s*[+＋]", text, re.I) else "",
        "signature_placeholders": sorted(token for token in placeholders if token in compact_text),
        "blank_signature_dates": bool(re.search(r"日期[:：]\s+日期[:：]", text) or re.search(r"日期[:：]\s*(?:\n|$)", text)),
        "responsibilities": {"buyer_integration": bool(re.search(r"买方.*?负责.*?系统集成", text, re.S)), "buyer_installation": bool(re.search(r"买方.*?负责.*?(?:卸货|起吊|就位|现场安装)", text, re.S)), "seller_not_integration": bool(re.search(r"卖方.*?不承担.*?系统集成", text, re.S))},
        "extraction_state": {"products": "EXTRACTED" if quantities else "EXTRACTION_FAILED", "configurations": "EXTRACTED" if configs else "EXTRACTION_FAILED"},
    }
'''

ENGINE_POSTPROCESS = r'''
# PM_CONTRACT_REVIEW_PATCH_20260716_V1
_ORIGINAL_BUILD_REVIEW_ITEMS_20260716 = build_review_items
_ORIGINAL_BUILD_BT09_DRAFT_20260716 = _build_bt09_draft
_ORIGINAL_RUN_REVIEW_20260716 = run_review


def _replace_review_item(items: List[Dict[str, Any]], replacement: Dict[str, Any]) -> None:
    for index, item in enumerate(items):
        if item.get("id") == replacement.get("id"):
            items[index] = replacement
            return
    items.append(replacement)


def _delivery_review_item_20260716(documents: DocumentSet, contract: Dict[str, Any], cqp: Dict[str, Any]) -> Dict[str, Any]:
    contract_map = {str(item.get("model", "")): int(item.get("weeks", 0) or 0) for item in contract.get("delivery_schedule", []) if item.get("model")}
    cqp_map = {str(item.get("model", "")): int(item.get("weeks", 0) or 0) for item in cqp.get("delivery_schedule", []) if item.get("model")}
    global_weeks = int(cqp.get("delivery_weeks", 0) or 0)
    models = sorted(set(contract_map) | set(cqp_map))
    sub_items = []
    mismatches = []
    missing = []
    for model in models:
        left = contract_map.get(model, 0)
        right = cqp_map.get(model, 0) or global_weeks
        if not left or not right:
            status, state = "UNDETERMINED", "EXTRACTION_FAILED"
            summary = f"合同 {left or '未提取'}周 / CQP {right or '未提取'}周。"
            missing.append(model)
        elif left != right:
            status, state = "MISMATCH", "MISMATCH"
            summary = f"合同 {left}周 / CQP {right}周，逐型号交期明确不一致。"
            mismatches.append(f"{model}: {left}周 vs {right}周")
        else:
            status, state = "PASS", "MATCH"
            summary = f"合同与CQP均为{left}周。"
        sub_items.append({"id": "delivery_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_"), "title": model, "status": status, "decision_state": state, "summary": summary, "values": {"合同": left or "未提取", "CQP": right or "未提取"}, "evidence": []})
    trigger_diff = bool(contract.get("delivery_trigger") and cqp.get("delivery_trigger") and contract.get("delivery_trigger") != cqp.get("delivery_trigger"))
    if mismatches:
        # Project policy keeps delivery differences non-blocking because BT09
        # always copies the contract schedule. Preserve per-model mismatches as
        # explicit evidence, but expose the aggregate item as a review warning.
        status, severity, summary = "WARNING", "warning", "合同与CQP存在逐型号交期差异（非阻塞，BT09以合同为准）：" + "；".join(mismatches)
    elif missing:
        status, severity, summary = "UNDETERMINED", "warning", "部分逐型号交期未提取：" + "、".join(missing)
    elif trigger_diff:
        status, severity, summary = "WARNING", "warning", "逐型号周数一致，但合同与CQP的交期起算条件不同；BT09以合同原文为准。"
    else:
        status, severity, summary = "PASS", "info", "合同与CQP逐型号交期及起算条件一致。"
    return _item(
        "delivery_period", CATEGORY_OTHER, "交付周期与起算条件", status, severity, summary,
        {"合同": "；".join(f"{model} {weeks}周" for model, weeks in contract_map.items()) or "未提取", "合同起算": contract.get("delivery_trigger") or "未提取", "CQP逐型号": "；".join(f"{model} {weeks}周" for model, weeks in cqp_map.items()) or "未提取", "CQP通用": cqp.get("delivery_time") or "未提取", "CQP起算": cqp.get("delivery_trigger") or "未提取", "差异": mismatches or "无", "BT09来源": "合同"},
        [], sub_items,
        decision_state="REVIEW_REQUIRED" if mismatches else ("EXTRACTION_FAILED" if missing else ("REVIEW_REQUIRED" if trigger_diff else "MATCH")),
    )


def build_review_items(documents: DocumentSet, contract: Dict[str, Any], cqp: Dict[str, Any], ta: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = _ORIGINAL_BUILD_REVIEW_ITEMS_20260716(documents, contract, cqp, ta)
    by_id = {item.get("id"): item for item in items}

    seller = by_id.get("seller_entity")
    if seller and seller.get("status") == "PASS" and seller.get("values", {}).get("合同号预期实体") == "无映射":
        seller["summary"] = "三份文件卖方主体一致；当前合同号前缀无映射，因此未声称完成前缀规则校验。"

    lps = by_id.get("lps_lite_naming")
    has_lps = any("LPS" in str(item.get("model", "")).upper() for item in ta.get("products", []))
    if lps and not has_lps:
        lps.update({"status": "INFO", "severity": "info", "decision_state": "NOT_APPLICABLE", "summary": "本合同不包含LPS/Lite+型号，本检查不适用。"})

    _replace_review_item(items, _delivery_review_item_20260716(documents, contract, cqp))

    conflicts = ta.get("quantity_conflicts", [])
    conflict_text = [f"{entry.get('model')}: {entry.get('values')}" for entry in conflicts]
    _replace_review_item(items, _item(
        "ta_internal_quantity_consistency", CATEGORY_PRODUCT, "TA内部数量一致性",
        "MISMATCH" if conflicts else "PASS", "blocker" if conflicts else "info",
        "TA同一型号出现互相矛盾的明确数量：" + "；".join(conflict_text) if conflicts else "TA未发现同一型号的明确数量自相矛盾。",
        {"冲突": conflict_text or "无", "采用数量": {entry.get("model"): entry.get("selected") for entry in conflicts}},
        [], decision_state="MISMATCH" if conflicts else "MATCH",
    ))

    red_flags = ta.get("technical_red_flags", [])
    _replace_review_item(items, _item(
        "ta_technical_red_flags", CATEGORY_PRODUCT, "TA显性技术参数风险",
        "MISMATCH" if red_flags else "PASS", "blocker" if red_flags else "info",
        "；".join(str(entry.get("detail", "")) for entry in red_flags) if red_flags else "未发现预设的显性技术参数风险。",
        {"问题": [entry.get("detail") for entry in red_flags] or "无"},
        [_evidence(documents.ta, "ta", entry.get("type", "技术参数"), [entry.get("quote", "")], page_hint=entry.get("page")) for entry in red_flags],
        decision_state="MISMATCH" if red_flags else "MATCH",
    ))

    arithmetic = []
    for product in cqp.get("products", []):
        diff = round(float(product.get("line_total_difference", 0) or 0), 2)
        if abs(diff) >= 0.005:
            arithmetic.append(f"{product.get('model')}: 数量×单价={product.get('expected_line_total'):.2f}，行总额={float(product.get('line_total', 0)):.2f}，差额={diff:.2f}")
    _replace_review_item(items, _item(
        "cqp_line_arithmetic", CATEGORY_OTHER, "CQP行金额算术校验",
        "WARNING" if arithmetic else "PASS", "warning" if arithmetic else "info",
        "；".join(arithmetic) if arithmetic else "CQP各产品行的数量×单价与行总额一致。",
        {"差异": arithmetic or "无"}, [], decision_state="REVIEW_REQUIRED" if arithmetic else "MATCH",
    ))

    completeness = []
    if str(cqp.get("customer_postal_code", "")).strip() == "000000":
        completeness.append("CQP客户邮政编码为占位值000000")
    if ta.get("quotation_number_blank"):
        completeness.append("TA页眉Quotation No.为空")
    _replace_review_item(items, _item(
        "document_field_completeness", CATEGORY_OTHER, "模板字段完整性",
        "WARNING" if completeness else "PASS", "warning" if completeness else "info",
        "；".join(completeness) if completeness else "未发现预设的模板占位字段问题。",
        {"问题": completeness or "无"}, [], decision_state="REVIEW_REQUIRED" if completeness else "MATCH",
    ))
    return items


def _build_bt09_draft(contract: Dict[str, Any], cqp: Dict[str, Any], ta: Dict[str, Any], items: Sequence[Dict[str, Any]], customer_master: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fields = _ORIGINAL_BUILD_BT09_DRAFT_20260716(contract, cqp, ta, items, customer_master)
    fields["payment_terms_verbatim"] = _clean_payment_verbatim(contract.get("payment_terms", {}).get("raw", ""))
    fields["draft_mode"] = "ready" if fields.get("ready") else "preview_only"
    return fields


def run_review(pdf_paths: List[str], customer_db_path: str = None, template_path: str = None, file_roles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    result = _ORIGINAL_RUN_REVIEW_20260716(pdf_paths, customer_db_path=customer_db_path, template_path=template_path, file_roles=file_roles)
    preview = result.get("bt09_draft", "")
    ready = bool(result.get("bt09_fields", {}).get("ready"))
    result["bt09_preview"] = preview
    result["bt09_draft"] = preview if ready else ""
    result["pipeline_completed"] = True
    result["review_passed"] = result.get("conclusion") == "Pass"
    result["review_blocked"] = result.get("conclusion") == "Blocked"
    return result
'''

LLM_RESPONSE_PATCH_OLD = "        parsed = extract_json_object(call_llm_chat(cfg, messages))\n"
LLM_RESPONSE_PATCH_NEW = '''        raw_response = call_llm_chat(cfg, messages)\n        if isinstance(raw_response, dict):\n            raw_response = raw_response.get("content") or raw_response.get("text") or raw_response.get("message") or ""\n        elif isinstance(raw_response, (tuple, list)):\n            raw_response = raw_response[0] if raw_response else ""\n        parsed = extract_json_object(to_str(raw_response))\n'''


LLM_CONSOLE_LOGGED_CALL = r'''
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
'''

LLM_CONSOLE_INSTALL = r'''
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
'''

SERVER_OLD = '''        result = run_review(pdf_paths, file_roles=file_roles)\n        result["ok"] = True\n        return result\n'''
SERVER_NEW = '''        result = run_review(pdf_paths, file_roles=file_roles)\n        # `ok` means the HTTP/pipeline operation completed, not that the legal review passed.\n        result["ok"] = True\n        result.setdefault("pipeline_completed", True)\n        result["review_passed"] = result.get("conclusion") == "Pass"\n        result["review_blocked"] = result.get("conclusion") == "Blocked"\n        return result\n'''

UPDATED_OLD_TEST = r'''
    def test_configuration_parser_rejects_dates_voltage_and_cross_page_carryover(self) -> None:
        parsed = ParsedPDF(
            "ta.pdf",
            [
                ParsedPage(1, 612, 792, "IRB 2600-20/1.65\n配置清单\n2026-01 日期\n220-230 V 电压\n3000-1 Controller"),
                ParsedPage(2, 612, 792, "3016-3 30m cable"),
            ],
        )
        configs = _extract_configurations(parsed, "ta")
        self.assertEqual([(item["model"], item["code"]) for item in configs], [("IRB 2600-20/1.65", "3000-1")])
'''
UPDATED_NEW_TEST = r'''
    def test_configuration_parser_rejects_noise_and_keeps_cross_page_continuation(self) -> None:
        parsed = ParsedPDF(
            "ta.pdf",
            [
                ParsedPage(1, 612, 792, "1. IRB 2600-20/1.65 Industry Robot\n配置清单\n2026-01 日期\n220-230 V 电压\n3000-1 Controller"),
                ParsedPage(2, 612, 792, "3016-3 30m cable"),
            ],
        )
        configs = _extract_configurations(parsed, "ta")
        self.assertEqual(
            [(item["model"], item["code"]) for item in configs],
            [("IRB 2600-20/1.65", "3000-1"), ("IRB 2600-20/1.65", "3016-3")],
        )
'''

NEW_TEST_CONTENT = r'''from __future__ import annotations

import unittest

from services import contract_review_engine as engine
from services.contract_review_knowledge import config_family, get_contract_review_knowledge
from services.pdf_evidence import ParsedPDF, ParsedPage, TextSpan


class ContractReviewRegression20260716Tests(unittest.TestCase):
    def setUp(self) -> None:
        get_contract_review_knowledge.cache_clear()

    def test_spaced_contract_number_is_normalized(self) -> None:
        parsed = ParsedPDF("contract.pdf", [ParsedPage(1, 612, 792, "销售合同\n合同编号：M 4 3 6 7 - 3 1 4 9")])
        self.assertEqual(engine.extract_contract(parsed)["contract_number"], "M4367-3149")

    def test_ta_range_uses_page_1_of_n(self) -> None:
        parsed = ParsedPDF("bundle.pdf", [
            ParsedPage(1, 612, 792, "销售合同 M4367-3149"),
            ParsedPage(13, 612, 792, "Technical Agreement 技术协议书 Doc No. 3.02.F03 Page 1 of 19"),
            ParsedPage(31, 612, 792, "Doc No. 3.02.F03 Page 19 of 19"),
            ParsedPage(32, 612, 792, "附件三 诚信条款"),
        ])
        self.assertEqual(engine._find_ta_start(parsed), 13)
        self.assertEqual(engine._find_ta_end(parsed, 13), 32)

    def test_inline_numbered_payment_clauses_keep_50_percent_method(self) -> None:
        result = engine._extract_payment_terms(
            "付款条件\nPurchase Order, 合同总价的百分之十，应在签订本合同后七个工作日内以电汇方式支付；"
            "2）合同总价的百分之四十，应在设备发货前七个工作日内以电汇方式支付。"
            "3）合同总价的百分之五十，应在设备发货前七个工作日内以ABB认可的电子银行承兑汇票方式支付。\n交货时间\n8周"
        )
        self.assertEqual(result["percentages"], [10.0, 40.0, 50.0])
        self.assertEqual(result["installments"][2]["method"], "银行承兑汇票")

    def test_cross_page_configuration_stays_with_previous_model(self) -> None:
        parsed = ParsedPDF("ta.pdf", [
            ParsedPage(1, 612, 792, "1. IRB 1100-4/0.58 Industry Robot\n3107-1 Collision detection"),
            ParsedPage(2, 612, 792, "3151-1 Program package\n2. IRB 1200-7/0.7 Gen2 Industry Robot\n3300-122 IRB 1200-7/0.7 Gen2"),
        ])
        configs = engine._extract_configurations(parsed, "ta")
        by_code = {item["code"]: item["model"] for item in configs}
        self.assertEqual(by_code["3151-1"], "IRB 1100-4/0.58")
        self.assertEqual(by_code["3300-122"], "IRB 1200-7/0.7 Gen2")

    def test_model_fragment_is_not_configuration_code(self) -> None:
        parsed = ParsedPDF("ta.pdf", [ParsedPage(1, 612, 792, "1. IRB 5710-90/2.7 Industry Robot\n/0.7Gen2 型工业机器人\n3151-1 Program package")])
        codes = [item["code"] for item in engine._extract_configurations(parsed, "ta")]
        self.assertNotIn("1200-7", codes)

    def test_independent_function_codes_are_not_one_conflict_family(self) -> None:
        get_contract_review_knowledge.cache_clear()
        # Both codes intentionally have no mutually-exclusive family. Empty
        # values therefore mean “do not infer a family conflict,” not equality.
        self.assertEqual(config_family("3107-1"), "")
        self.assertEqual(config_family("3151-1"), "")
        result = engine._config_match(
            {"model": "IRB 1100-4/0.58", "code": "3151-1", "description": "Program package"},
            [{"model": "IRB 1100-4/0.58", "code": "3107-1", "description": "Collision detection"}],
            "",
        )
        self.assertFalse(result["matched"])
        self.assertIsNone(result["conflict"])

    def test_quantity_contradiction_is_preserved(self) -> None:
        parsed = ParsedPDF("ta.pdf", [
            ParsedPage(1, 612, 792, "甲方采购 1 台 IRB 5710-90/2.7"),
            ParsedPage(2, 612, 792, "根据买方选择提供 11 台 IRB 5710-90/2.7"),
            ParsedPage(3, 612, 792, "卖方供货 1 台 IRB 5710-90/2.7"),
        ])
        details = engine._extract_model_quantity_details(parsed)
        self.assertEqual(details["selected"]["IRB 5710-90/2.7"], 1)
        self.assertEqual(details["conflicts"][0]["values"], [1, 11])

    def test_visual_checkbox_selects_ddp(self) -> None:
        page = ParsedPage(
            2, 612, 792,
            "买方工厂的到货价\n卖方工厂出厂价",
            spans=[
                TextSpan("买方工厂的到货价", (100, 100, 250, 112)),
                TextSpan("卖方工厂出厂价", (100, 140, 250, 152)),
            ],
            checkbox_marks=[
                {"bbox": [82, 101, 92, 111], "checked": True},
                {"bbox": [82, 141, 92, 151], "checked": False},
            ],
        )
        result = engine._detect_contract_incoterm(ParsedPDF("contract.pdf", [page]))
        self.assertEqual(result["selected"], "DDP")

    def test_ta_red_flags_find_unit_and_blank_safemove(self) -> None:
        parsed = ParsedPDF("ta.pdf", [ParsedPage(22, 612, 792, "位置重复精度：0.011m\nSafemove 功能：\n5.机器人在系统中的功能")])
        kinds = {item["type"] for item in engine._extract_ta_technical_red_flags(parsed)}
        self.assertEqual(kinds, {"repeatability_unit", "blank_safemove"})

    def test_llm_console_forwards_return_metadata(self) -> None:
        from services.llm_console import logged_llm_call

        seen = {}

        def fake_call(config, messages, *, return_metadata=False):
            seen["return_metadata"] = return_metadata
            return {"content": "ok", "finish_reason": "stop", "usage": {}}

        response = logged_llm_call(fake_call, {}, [], return_metadata=True)
        self.assertTrue(seen["return_metadata"])
        self.assertEqual(response["content"], "ok")


if __name__ == "__main__":
    unittest.main()
'''


def patch_pdf_evidence(source: str) -> str:
    source = replace_top_level_class(source, "ParsedPage", PDF_PAGE_CLASS)
    source = replace_top_level_function(source, "parse_pdf_with_evidence", PDF_PARSE_FUNCTION)
    return source


def patch_knowledge(source: str) -> str:
    source = replace_exact(
        source,
        '_DEFAULT_COMMERCIAL_ONLY_CODES: Set[str] = {"448-125"}',
        '_DEFAULT_COMMERCIAL_ONLY_CODES: Set[str] = {"448-125", "3144-1", "3112-1", "3371-27"}',
        "商务/打包代码集合",
    )
    source = replace_top_level_function(source, "_parse_mapping_markdown", KNOWLEDGE_PARSE)
    return source


def patch_engine(source: str) -> str:
    """Rebase the fixes onto cb91c5dd without discarding its OCR/LLM hardening."""
    source = replace_top_level_function(source, "_find_ta_start", TA_START)
    source = insert_before_function_once(source, "_resolve_documents", "PM_TA_RANGE_PATCH_20260716", TA_END_HELPER)
    source = replace_top_level_function(source, "_resolve_documents", RESOLVE_DOCUMENTS)

    # Keep cb91c5dd's stronger Annex-2 source selection and selective OCR.
    source = replace_top_level_function(source, "_looks_like_configuration_code", LOOKS_CONFIG)
    source = insert_before_function_once(source, "_extract_model_quantities", "PM_QUANTITY_EVIDENCE_PATCH_20260716", QUANTITY_HELPER)
    source = replace_top_level_function(source, "_extract_model_quantities", QUANTITY_WRAPPER)
    source = replace_top_level_function(source, "_extract_payment_terms", PAYMENT_TERMS)
    source = insert_before_function_once(source, "_extract_configurations", "PM_CONFIG_SECTION_PATCH_20260716", CONFIG_HELPER)
    source = replace_top_level_function(source, "_extract_configurations", CONFIG_EXTRACT)
    source = replace_top_level_function(source, "_model_section_text", MODEL_SECTION)
    source = insert_before_function_once(source, "_detect_contract_incoterm", "PM_WARRANTY_PROSE_PATCH_20260716", WARRANTY_HELPER)
    source = replace_top_level_function(source, "_detect_contract_incoterm", INCOTERM_DETECT)
    source = insert_before_function_once(source, "extract_contract", "PM_EXTRACTION_HELPERS_PATCH_20260716", EXTRACTION_HELPERS)

    # Patch only the two stale statements inside cb91c5dd's extract_contract.
    contract_number_old = (
        '    contract_match = re.search(r"\\b([MK])\\s*(\\d{4})\\s*-\\s*(\\d{4})\\b", text, re.I)\n'
        '    contract_number = "" if not contract_match else f"{contract_match.group(1).upper()}{contract_match.group(2)}-{contract_match.group(3)}"\n'
    )
    if '    contract_number = _extract_business_number(text)\n' not in source:
        if contract_number_old not in source:
            raise RuntimeError("未找到当前版本的合同号抽取语句")
        source = source.replace(contract_number_old, '    contract_number = _extract_business_number(text)\n', 1)
    source = replace_exact(
        source,
        '        "incoterm_detection": _detect_contract_incoterm(transport_text),\n',
        '        "incoterm_detection": _detect_contract_incoterm(parsed),\n',
        "Incoterm图像复选框输入",
    )

    source = replace_top_level_function(source, "extract_cqp", EXTRACT_CQP)
    source = insert_before_function_once(source, "extract_ta", "PM_TA_RED_FLAGS_PATCH_20260716", TA_RED_FLAGS)
    source = replace_top_level_function(source, "extract_ta", EXTRACT_TA)
    source = append_once(source, "PM_CONTRACT_REVIEW_PATCH_20260716_V1", ENGINE_POSTPROCESS)
    return source


def patch_llm_console(source: str) -> str:
    source = replace_top_level_function(source, "logged_llm_call", LLM_CONSOLE_LOGGED_CALL)
    source = replace_top_level_function(source, "install_contract_review_console", LLM_CONSOLE_INSTALL)
    return source


def patch_server(source: str) -> str:
    if 'result["review_blocked"] = result.get("conclusion") == "Blocked"' in source:
        return source
    pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)result = run_review\(pdf_paths, file_roles=file_roles\)\n'
        r'(?P=indent)result\["ok"\] = True\n'
        r'(?P=indent)return result$'
    )
    match = pattern.search(source)
    if not match:
        raise RuntimeError("未找到合同审核API返回块")
    indent = match.group("indent")
    replacement = (
        f'{indent}result = run_review(pdf_paths, file_roles=file_roles)\n'
        f'{indent}# `ok` means the HTTP/pipeline operation completed, not that the legal review passed.\n'
        f'{indent}result["ok"] = True\n'
        f'{indent}result.setdefault("pipeline_completed", True)\n'
        f'{indent}result["review_passed"] = result.get("conclusion") == "Pass"\n'
        f'{indent}result["review_blocked"] = result.get("conclusion") == "Blocked"\n'
        f'{indent}return result'
    )
    return source[:match.start()] + replacement + source[match.end():]


def patch_tests(source: str) -> str:
    new_name = "test_configuration_parser_rejects_noise_and_keeps_cross_page_continuation"
    if new_name in source:
        return source
    old_name = "test_configuration_parser_rejects_dates_voltage_and_cross_page_carryover"
    if old_name not in source:
        raise RuntimeError("未找到旧的跨页配置测试")
    source = source.replace(old_name, new_name, 1)
    source = source.replace(
        'ParsedPage(1, 612, 792, "IRB 2600-20/1.65\n配置清单',
        'ParsedPage(1, 612, 792, "1. IRB 2600-20/1.65 Industry Robot\n配置清单',
        1,
    )
    old_assert = 'self.assertEqual([(item["model"], item["code"]) for item in configs], [("IRB 2600-20/1.65", "3000-1")])'
    new_assert = (
        'self.assertEqual(\n'
        '            [(item["model"], item["code"]) for item in configs],\n'
        '            [("IRB 2600-20/1.65", "3000-1"), ("IRB 2600-20/1.65", "3016-3")],\n'
        '        )'
    )
    if old_assert not in source:
        raise RuntimeError("未找到旧的跨页配置断言")
    return source.replace(old_assert, new_assert, 1)


def patch_local_console_wrappers(root: Path, transformed: Dict[Path, str]) -> None:
    """Best-effort compatibility for local-only console wrappers.

    The debug report showed a local `install_contract_review_console.<locals>.wrapped`
    rejecting `return_metadata`. That wrapper is not present on GitHub master, so
    patch it only when it exists in the user's checkout.
    """
    for path in (root / "backend").rglob("*.py"):
        if path in transformed or not path.is_file():
            continue
        source = read_text(path)
        if "install_contract_review_console" not in source or "def wrapped(" not in source:
            continue
        updated = re.sub(
            r"def wrapped\(([^)]*)\):",
            lambda match: match.group(0) if "*args" in match.group(1) else f"def wrapped({match.group(1)}, *args, **kwargs):",
            source,
            count=1,
        )
        updated = re.sub(
            r"return (?:original|current)\(config, messages\)",
            lambda match: match.group(0).replace("config, messages", "config, messages, *args, **kwargs"),
            updated,
            count=1,
        )
        if updated != source:
            transformed[path] = updated


def git_dirty_targets(root: Path, paths: Iterable[Path]) -> List[str]:
    if not (root / ".git").exists():
        return []
    relative = [str(path.relative_to(root)).replace(os.sep, "/") for path in paths if path.exists()]
    if not relative:
        return []
    result = _run(["git", "status", "--porcelain", "--", *relative], root, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


def current_commit(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    result = _run(["git", "rev-parse", "HEAD"], root, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def restore(backups: Dict[Path, Path]) -> None:
    for target, backup in backups.items():
        if backup.exists():
            shutil.copy2(backup, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply PM_workspace contract-review fixes")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="PM_workspace repository root")
    parser.add_argument("--dry-run", action="store_true", help="validate and show files without writing")
    parser.add_argument("--force", action="store_true", help="allow patching dirty target files; backups are still created")
    parser.add_argument("--no-tests", action="store_true", help="skip focused unittest run")
    parser.add_argument("--keep-on-failure", action="store_true", help="do not roll back if compile/tests fail")
    args = parser.parse_args()

    root = find_repo_root(args.root)
    paths = {relative: root / relative for relative in TARGETS}
    missing = [relative for relative, path in paths.items() if not path.is_file()]
    if missing:
        raise SystemExit("缺少目标文件：" + "、".join(missing))

    dirty = git_dirty_targets(root, list(paths.values()))
    if dirty and not args.force:
        print("以下目标文件已有未提交修改。为避免覆盖，补丁已停止：", file=sys.stderr)
        print("\n".join(dirty), file=sys.stderr)
        print("确认已备份后，可使用 --force。", file=sys.stderr)
        return 2

    commit = current_commit(root)
    if commit and commit != EXPECTED_BASE_COMMIT:
        print(f"[warning] 当前HEAD为 {commit[:12]}，基准为 {EXPECTED_BASE_COMMIT[:12]}；将依靠AST锚点安全应用。")

    transformers: Dict[str, Callable[[str], str]] = {
        "backend/services/pdf_evidence.py": patch_pdf_evidence,
        "backend/services/contract_review_knowledge.py": patch_knowledge,
        "backend/services/contract_review_engine.py": patch_engine,
        "backend/services/llm_console.py": patch_llm_console,
        "backend/server.py": patch_server,
        "backend/tests/test_contract_review.py": patch_tests,
    }
    transformed: Dict[Path, str] = {}
    before_hashes: Dict[Path, str] = {}
    for relative, transform in transformers.items():
        path = paths[relative]
        source = read_text(path)
        before_hashes[path] = sha256_text(source)
        updated = transform(source)
        ast.parse(updated)  # fail before touching disk
        if updated != source:
            transformed[path] = updated

    new_test_path = root / NEW_TEST
    if not new_test_path.exists() or read_text(new_test_path) != NEW_TEST_CONTENT:
        transformed[new_test_path] = NEW_TEST_CONTENT
    patch_local_console_wrappers(root, transformed)
    for path, content in transformed.items():
        if path.suffix == ".py":
            ast.parse(content)

    if args.dry_run:
        print("Dry run通过。将修改：")
        for path in transformed:
            print(" -", path.relative_to(root))
        return 0
    if not transformed:
        print("补丁已存在，无需重复修改。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = root / ".pm_patch_backups" / f"contract_review_{stamp}"
    backups: Dict[Path, Path] = {}
    for target in transformed:
        if target.exists():
            backup = backup_root / target.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            backups[target] = backup

    try:
        for path, content in transformed.items():
            atomic_write(path, content)
        compile_targets = [str(path.relative_to(root)) for path in transformed if path.suffix == ".py"]
        result = _run([sys.executable, "-m", "py_compile", *compile_targets], root, check=False)
        if result.returncode != 0:
            raise RuntimeError("Python编译失败：\n" + result.stdout + result.stderr)
        if not args.no_tests:
            # The tests import ``services.*`` as a top-level package, so they must
            # run with ``backend`` as the working directory/PYTHONPATH. Running
            # ``backend.tests...`` from the repository root makes Python search
            # for a non-existent top-level ``services`` package.
            backend_dir = root / "backend"
            test_env = os.environ.copy()
            existing_pythonpath = test_env.get("PYTHONPATH", "")
            test_env["PYTHONPATH"] = str(backend_dir) + (
                os.pathsep + existing_pythonpath if existing_pythonpath else ""
            )
            test_commands = [
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_contract_review*.py",
                ],
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_llm_console.py",
                ],
            ]
            test_outputs: List[str] = []
            for command in test_commands:
                test_result = _run(
                    command,
                    backend_dir,
                    check=False,
                    env=test_env,
                )
                if test_result.returncode != 0:
                    rendered = " ".join(command)
                    raise RuntimeError(
                        "回归测试失败：\n"
                        + f"命令：{rendered}\n"
                        + test_result.stdout
                        + test_result.stderr
                    )
                output = (test_result.stdout + test_result.stderr).strip()
                if output:
                    test_outputs.append(output)
            if test_outputs:
                print("\n".join(test_outputs))
    except Exception as exc:
        if not args.keep_on_failure:
            restore(backups)
            if new_test_path in transformed and new_test_path not in backups and new_test_path.exists():
                new_test_path.unlink()
            print("验证失败，已自动回滚。", file=sys.stderr)
        else:
            print("验证失败，但按 --keep-on-failure 保留修改。", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    print("补丁应用成功。未执行 git add/commit/reset/checkout。")
    print("备份目录：", backup_root.relative_to(root))
    print("修改文件：")
    for path in transformed:
        print(" -", path.relative_to(root))
    print("建议重新运行同一组合同/CQP并导出新的调试报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
