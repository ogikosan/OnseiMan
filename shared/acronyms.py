"""聞き取った文の英字の略語を大文字にする（Haas-ja は英字をすべて小文字で書く: cpu → CPU）。"""
from __future__ import annotations

import re

# 大文字で書く略語（Haas は英字をすべて小文字で書く）。優先語に入れた言葉は、その書き方が優先される
ACRONYMS = (
    "AI", "API", "CPU", "GPU", "LLM", "PC", "OS", "USB", "SSD", "HDD", "URL", "PDF", "CSV", "SNS",
    "SSH", "WSL", "CUDA", "VRAM", "RAM", "IT", "IP", "LAN", "DM", "QR", "UI", "UX", "PR", "CM",
    "NG", "OK", "HP", "ID", "PIN", "FAQ", "HTML", "CSS", "JSON", "SQL", "AWS", "MCP", "DGX", "RTX",
    "GB", "MB", "TB", "KB", "TV", "PS", "VR", "AR", "GPT",
)
_ACRONYM_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted((a.lower() for a in ACRONYMS), key=len, reverse=True)) + r")(?![A-Za-z])")


def fix_acronyms(text: str) -> str:
    """小文字で出た略語を大文字にする（llm → LLM）。前後が英字のとき（apple など）は触らない。"""
    return _ACRONYM_RE.sub(lambda m: m.group(1).upper(), text)
