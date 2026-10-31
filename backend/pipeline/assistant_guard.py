"""Server-side guard for assistant questions.

The assistant has no data access and no tools, so it cannot see any
company's rows or run anything. This guard is the outer fence: it
refuses shape-obvious SQL, code, bulk-destructive commands, and
requests for other companies' data before a single token is spent,
so every attempt is logged without costing the company.
"""

from __future__ import annotations

import re

SQL = "sql"
CODE = "code"
DESTRUCTIVE = "destructive"
CROSS_TENANT = "cross_tenant"

_SQL_SHAPES = [
    r"\bselect\b.+\bfrom\b",
    r"\binsert\s+into\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\bdelete\s+from\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bunion\s+select\b",
    r";\s*(drop|delete|update|insert|truncate)\b",
    r"\binformation_schema\b",
]
_ALWAYS_CODE = [
    r"```",
    r"\bimport\s+(os|sys|subprocess|shutil)\b",
    r"\bfrom\s+(os|subprocess)\s+import\b",
    r"\b(eval|exec)\s*\(",
    r"__import__",
    r"\brm\s+-rf?\b",
    r"\bcurl\b.+\|\s*(ba)?sh\b",
    r"\bwget\b.+\|\s*(ba)?sh\b",
    r"<script\b",
    r"<\?php",
]
_DESTRUCTIVE_COMMANDS = [
    r"\b(delete|drop|wipe|truncate|erase)\s+(all|every)\b",
    r"\b(delete|drop|wipe|truncate|erase)\s+the\s+"
    r"(data|database|tables?|records?|rows?|compan\w*|organi\w*)\b",
    r"\bdrop\s+everything\b",
]
_CROSS_TENANT = [
    r"\b(other|another|different)\s+(compan\w*|custom\w*|client\w*|organi\w*)",
    r"\ball\s+(compan\w*|custom\w*|clients?|users?)\s+(data|records?|info)",
    r"\blist\s+all\s+(users?|compan\w*|custom\w*|organi\w*)",
    r"\bshow\s+me\s+(other|another|different)\b",
    r"\b(download|export|dump)\s+(all\s+)?(other|another)\b",
]
_QUESTION_PHRASE = re.compile(
    r"^\s*(how|what|why|when|where|which|can|could|is|are|do|does)\b|\?\s*$",
    re.IGNORECASE,
)


def _matches(patterns: list, text: str) -> bool:
    return any(
        re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        for pattern in patterns
    )


def inspect_question(text: str) -> str | None:
    """Return a flag reason, or None when the question may be asked."""
    if _matches(_ALWAYS_CODE, text):
        return CODE
    if _matches(_CROSS_TENANT, text):
        return CROSS_TENANT
    if _QUESTION_PHRASE.search(text or ""):
        return None
    if _matches(_SQL_SHAPES, text):
        return SQL
    if _matches(_DESTRUCTIVE_COMMANDS, text):
        return DESTRUCTIVE
    return None
