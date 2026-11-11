"""Email templates: {{variables}} with optional fallbacks, and {spin|tax}.

    Hi {{first_name|there}}, {we help|we work with} {{industry}} companies like {{company}}.

Spintax is resolved first (innermost group first, so it can nest), then variables. Text inside {{…}}
is never treated as spintax, so a fallback can contain "|"-free text safely.
"""

import random
import re
from typing import Any

from app.models import Lead

VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?:\|([^{}]*))?\}\}")
# A single-brace group holding at least one "|" and no other braces: the innermost spintax group.
SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")

FIXED_FIELDS = ["email", "first_name", "last_name", "company", "country"]


def lead_fields(lead: Lead) -> dict[str, str]:
    fields: dict[str, str] = {k: str(v) for k, v in (lead.custom or {}).items() if v not in (None, "")}
    for name in FIXED_FIELDS:
        value = getattr(lead, name)
        if value:
            fields[name] = value
    return fields


def spin(text: str, rng: random.Random) -> str:
    variables: list[str] = []

    def hide(m: re.Match[str]) -> str:
        variables.append(m.group(0))
        return f"\x00{len(variables) - 1}\x00"

    text = VARIABLE.sub(hide, text)
    while True:
        text, n = SPIN.subn(lambda m: rng.choice(m.group(1).split("|")), text)
        if n == 0:
            break
    return _PLACEHOLDER.sub(lambda m: variables[int(m.group(1))], text)


def fill(text: str, fields: dict[str, Any]) -> str:
    def value(m: re.Match[str]) -> str:
        found = fields.get(m.group(1))
        if found not in (None, ""):
            return str(found)
        return (m.group(2) or "").strip()

    return VARIABLE.sub(value, text)


def render(text: str, fields: dict[str, Any], rng: random.Random) -> str:
    return fill(spin(text, rng), fields)


def variables_used(*texts: str) -> set[str]:
    return {m.group(1) for t in texts for m in VARIABLE.finditer(t)}


def footer(sender_name: str, business_name: str, postal_address: str) -> str:
    # Plan section 16: real sender, postal address (CAN-SPAM, CASL) and a reply-to-opt-out line.
    return (
        f"--\n{sender_name} · {business_name}\n{postal_address}\n"
        "If this isn't relevant, just reply and I won't email again."
    )


def compose(
    subject: str, body: str, fields: dict[str, Any], sender_name: str, footer_text: str, rng: random.Random
) -> tuple[str, str]:
    """The final subject and plain-text body for one recipient."""
    fields = {"sender_name": sender_name, **fields}
    rendered_subject = " ".join(render(subject, fields, rng).split())
    rendered_body = render(body, fields, rng).rstrip()
    return rendered_subject, f"{rendered_body}\n\n{footer_text}\n"
