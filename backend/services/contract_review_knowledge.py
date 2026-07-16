# -*- coding: utf-8 -*-
"""Load maintainable contract-review knowledge from backend/config.

The former agent prompt files are treated as a business-rule knowledge base.  The
review engine remains deterministic, but configuration aliases and explanatory
rules can be extended in Markdown without editing Python for every new ABB code.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Sequence, Set, Tuple


# Fallbacks keep the service functional when a packaged deployment omits the
# editable prompt directory.  Values from Markdown are merged on top.
_FALLBACK_ALIASES: Dict[str, Tuple[str, ...]] = {
    "209-1": ("ABB orange standard", "ABB标准橙色"),
    "209-202": ("ABB Graphite White std", "ABB石墨白", "ABB标准白色"),
    "3350-400": ("Manipulator protection Base 40", "防护等级 Base 40", "IP40"),
    "3350-540": ("Manipulator protection Base 54", "防护等级 Base 54", "IP54"),
    "3350-670": ("Manipulator protection Base 67", "防护等级 Base 67", "IP67"),
    "3352-10": ("Foundry Plus2 67", "铸造版 Plus2 67"),
    "3000-105": ("OmniCore E10",),
    "3000-210": ("OmniCore V100XT",),
    "3000-310": ("OmniCore V250XT",),
    "3000-410": ("OmniCore V400XT",),
    "3016-1": ("FlexPendant 3m", "示教器3米电缆"),
    "3016-2": ("FlexPendant 10m", "示教器10米电缆"),
    "3016-3": ("FlexPendant 30m", "示教器30米电缆"),
    "3200-2": ("Manipulator cable 7m", "本体电缆7米", "Floor cable 7m"),
    "3200-3": ("Manipulator cable 15m", "本体电缆15米", "Floor cable 15m"),
    "3032-1": ("24V 16in/16out digital I/O board", "24V 16入16出数字量I/O板", "I/O板"),
    "3107-1": ("Collision detection", "碰撞检测"),
    "3113-1": ("Path Recovery", "路径恢复"),
    "3114-1": ("Multitasking", "多任务"),
    "3120-2": ("Essential app package", "基础应用包", "基础功能包"),
    "3151-1": ("Program package", "程序包", "独立应用程序包"),
    "3416-2": ("Arc Welding", "弧焊"),
    "3043-11": ("SafeMove Standard", "SafeMove标准版"),
    "438-1": ("Standard Warranty", "标准质保", "标准保修"),
    "438-102": ("Lite Warranty", "Lite 标准质保"),
}

# Codes known to be commercial metadata rather than TA technical scope.  This is
# intentionally narrow: packages such as 3120-2 and 3151-1 may be technical and
# must not be silently ignored merely because their name contains "package".
_DEFAULT_COMMERCIAL_ONLY_CODES: Set[str] = {"448-125"}

_TERM_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("flexpendant", "示教器"),
    ("manipulator", "机器人本体"),
    ("robotarm", "机器人本体"),
    ("controller", "控制器"),
    ("operatingtemperature", "工作温度"),
    ("mainsvoltage", "电源电压"),
    ("cablegland", "电缆密封接头"),
    ("collisiondetection", "碰撞检测"),
    ("pathrecovery", "路径恢复"),
    ("multitasking", "多任务"),
    ("safemovestandard", "safemove标准版"),
)


@dataclass(frozen=True)
class ContractReviewKnowledge:
    aliases_by_code: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    family_by_code: Dict[str, str] = field(default_factory=dict)
    commercial_only_codes: Set[str] = field(default_factory=set)
    rule_context: str = ""
    source_files: Tuple[str, ...] = ()


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = re.sub(r"[\s_()（）\[\]【】,，。.:：;；/\\+＋-]+", "", text)
    for english, chinese in _TERM_ALIASES:
        text = text.replace(english, chinese)
    return text


def normalize_alias_text(value: str) -> str:
    """Public normalization used by deterministic semantic matching."""
    return _normalize_text(value)


def _split_alias_cell(value: str) -> List[str]:
    value = re.sub(r"<br\s*/?>", "/", value or "", flags=re.I)
    parts = re.split(r"\s*(?:/|；|;|、)\s*", value)
    return [part.strip(" `*\t") for part in parts if part.strip(" `*\t")]


def _read_text(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _parse_mapping_markdown(text: str) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    aliases: Dict[str, Set[str]] = {}
    families: Dict[str, str] = {}
    current_family = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{2,6}\s+(.*)$", line)
        if heading:
            current_family = re.sub(r"^\d+(?:[.]\d+)*\s*", "", heading.group(1)).strip()
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


def _knowledge_directory() -> str:
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(backend_dir, "config", "contract_checker_prompt")


@lru_cache(maxsize=1)
def get_contract_review_knowledge() -> ContractReviewKnowledge:
    """Load aliases and rule text once per process.

    Runtime deployments can restart the backend after editing Markdown.  A
    process-level cache avoids rereading several large rule files per review.
    """
    aliases: Dict[str, Set[str]] = {code: set(values) for code, values in _FALLBACK_ALIASES.items()}
    families: Dict[str, str] = {}
    commercial_codes = set(_DEFAULT_COMMERCIAL_ONLY_CODES)
    context_parts: List[str] = []
    source_files: List[str] = []

    directory = _knowledge_directory()
    preferred = (
        "Agent_Prompt.txt",
        "合同审核规则手册.md",
        "配置代码映射表.md",
        "历史案例库.md",
        "BT09邮件模板库.md",
    )
    for filename in preferred:
        path = os.path.join(directory, filename)
        text = _read_text(path)
        if not text:
            continue
        source_files.append(filename)
        context_parts.append(f"===== {filename} =====\n{text}")
        if filename.endswith("配置代码映射表.md"):
            parsed_aliases, parsed_families = _parse_mapping_markdown(text)
            for code, values in parsed_aliases.items():
                aliases.setdefault(code, set()).update(values)
            families.update(parsed_families)

    normalized_aliases = {
        code: tuple(sorted({alias.strip() for alias in values if alias.strip()}))
        for code, values in aliases.items()
    }
    return ContractReviewKnowledge(
        aliases_by_code=normalized_aliases,
        family_by_code=families,
        commercial_only_codes=commercial_codes,
        # The full text is useful to the optional LLM, but cap it so a malformed
        # or duplicated knowledge file cannot create an unbounded prompt.
        rule_context="\n\n".join(context_parts)[:50000],
        source_files=tuple(source_files),
    )


def aliases_for_code(code: str, description: str = "") -> Tuple[str, ...]:
    knowledge = get_contract_review_knowledge()
    output = list(knowledge.aliases_by_code.get(str(code or ""), ()))
    if description:
        output.append(str(description))
    return tuple(dict.fromkeys(item for item in output if item))


def config_family(code: str) -> str:
    knowledge = get_contract_review_knowledge()
    code = str(code or "")
    family = knowledge.family_by_code.get(code, "")
    if family:
        return family
    # Prefix families are a conservative fallback for common ABB option groups.
    prefix = code.split("-", 1)[0]
    return {
        "209": "颜色",
        "3000": "控制器",
        "3016": "示教器",
        "3032": "I/O",
        "3200": "本体电缆",
        "3350": "防护等级",
        "3352": "防护等级",
        "438": "质保",
    }.get(prefix, "")


def is_commercial_only_config(code: str, description: str = "") -> bool:
    knowledge = get_contract_review_knowledge()
    if str(code or "") in knowledge.commercial_only_codes:
        return True
    key = _normalize_text(description)
    return any(token in key for token in ("deliveryproject", "交付项目", "commercialbundle", "商务打包"))
