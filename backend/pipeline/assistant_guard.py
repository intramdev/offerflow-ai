"""Server-side guard for assistant questions.

The assistant has no data access and no tools, so it cannot see any
company's rows or run anything. This guard is the outer fence: it
refuses shape-obvious SQL, code, bulk-destructive commands, and
requests for other companies' data before a single token is spent,
so every attempt is logged without costing the company.

Two scopes: the general help chat stays strict, while the import
preview assistant understands domain language — selecting a price
from a line or dropping header lines from the open draft is the
job there, not an attack. Scope never grants tools, data access,
or tenants; it only reads the same words with draft context.
"""

from __future__ import annotations

import re

SQL = "sql"
CODE = "code"
DESTRUCTIVE = "destructive"
CROSS_TENANT = "cross_tenant"

CHAT = "chat"
PREVIEW = "preview"

# Hard SQL: never legitimate phrasing in either scope.
_SQL_HARD = [
    r"\binsert\s+into\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bunion\s+select\b",
    r";\s*(drop|delete|update|insert|truncate)\b",
    r"\binformation_schema\b",
]
# Soft SQL: "select A from B" / "delete from B" is also how users
# describe interpretation ("select the price from the final
# number"). The preview scope exempts it only for domain targets.
_SQL_SOFT_SELECT = re.compile(r"\bselect\b(.+?)\bfrom\b(.+)", re.IGNORECASE | re.DOTALL)
_SQL_SOFT_DELETE = re.compile(r"\bdelete\s+from\b", re.IGNORECASE)
_SQL_PUNCTUATION = re.compile(r"[;]|--[^\n]*$|/\*|\*/|\bunion\b", re.IGNORECASE)
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
    r"^\s*(how|what|why|when|where|which|can|could|is|are|do|does"
    r"|come|cosa|perch[eé]|quando|dove|qual[ei]|quanto|puoi|pu[òo])\b"
    r"|\?\s*$",
    re.IGNORECASE,
)

# Domain vocabulary for the preview scope (English + Italian). These
# words describe the open draft's own lines and fields — never other
# tenants, tables, or the database.
_DOMAIN_FIELD = re.compile(
    r"\b(price|prices|quantity|quantities|number|numbers|amount|amounts"
    r"|currency|currencies|grade|grades|capacity|model|models|value|values"
    r"|name|names|date|dates|example|examples|total|totals"
    r"|prezzo|prezzi|quantit[àa]|numero|numeri|importo|importi|valuta|valute"
    r"|grado|gradi|capacit[àa]|modello|modelli|valore|valori|nome|nomi"
    r"|data|date|esempio|esempi|totale|totali)\b",
    re.IGNORECASE,
)
_LINE_SCOPED = re.compile(
    r"\b(lines?|headers?|products?|rows?|records?|sections?"
    r"|righe?|intestazioni?|prodotti?|record|sezioni?)\b",
    re.IGNORECASE,
)
_GLOBAL_NOUNS = re.compile(
    r"\b(data|database|databases|tables?|compan\w*|organi\w*|everything"
    r"|dati|tabelle?|aziende?)\b",
    re.IGNORECASE,
)
_TENANT_NOUNS = re.compile(
    r"\b(compan\w*|custom\w*|client\w*|organi\w*|users?|accounts?"
    r"|aziende?|clienti?)\b",
    re.IGNORECASE,
)
_SCOPE_ANCHOR = re.compile(
    r"\bthis\s+(draft|preview|file|message|import|page|list)\b"
    r"|\bin\s+this\b"
    r"|\b(each|every)\s+product\s+lines?\b"
    r"|\bproduct\s+lines?\b"
    r"|\bfinal\s+numbers?\b"
    r"|\bheader\s+lines?\b"
    r"|\bquesta\s+(bozza|anteprima)\b"
    r"|\bin\s+questa\b"
    r"|\bogni\s+riga\b",
    re.IGNORECASE,
)


def _matches(patterns: list, text: str) -> bool:
    return any(
        re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        for pattern in patterns
    )


def _preview_select_allowed(text: str) -> bool:
    """Allow "select <field> from <domain target>" in the preview scope.

    Real SQL keeps its tells: wildcards, stacked statements, comments,
    unions, and non-domain targets all stay blocked.
    """
    match = _SQL_SOFT_SELECT.search(text)
    if match is None:
        return False
    if "*" in text or _SQL_PUNCTUATION.search(text):
        return False
    selected, target = match.group(1), match.group(2)
    if _TENANT_NOUNS.search(target) or _GLOBAL_NOUNS.search(target):
        return False
    return bool(
        _DOMAIN_FIELD.search(selected) and _DOMAIN_FIELD.search(target)
    )


def _preview_exclusion_allowed(text: str) -> bool:
    """Allow reversible line/record exclusions in the preview scope."""
    if _GLOBAL_NOUNS.search(text):
        return False
    return bool(_LINE_SCOPED.search(text))


def _preview_example_allowed(text: str) -> bool:
    """Allow "another example" requests anchored to the open preview."""
    if _TENANT_NOUNS.search(text):
        return False
    return bool(_SCOPE_ANCHOR.search(text))


def inspect_question(text: str, *, scope: str = CHAT) -> str | None:
    """Return a flag reason, or None when the question may be asked.

    scope="preview" reads the message with import-draft context: typed
    interpretation, reversible classification/exclusion, and example
    requests inside the open draft pass, while code shapes, hard SQL,
    global destruction, and other-tenant access stay blocked.
    """
    content = text or ""
    if _matches(_ALWAYS_CODE, content):
        return CODE
    preview = scope == PREVIEW
    if _matches(_CROSS_TENANT, content):
        if not (preview and _preview_example_allowed(content)):
            return CROSS_TENANT
    if _QUESTION_PHRASE.search(content):
        return None
    if _matches(_SQL_HARD, content):
        return SQL
    if _SQL_SOFT_SELECT.search(content) or _SQL_SOFT_DELETE.search(content):
        if preview and (
            _preview_select_allowed(content)
            or (
                _SQL_SOFT_DELETE.search(content)
                and _preview_exclusion_allowed(content)
            )
        ):
            pass
        else:
            return SQL
    if _matches(_DESTRUCTIVE_COMMANDS, content):
        if not (preview and _preview_exclusion_allowed(content)):
            return DESTRUCTIVE
    return None
