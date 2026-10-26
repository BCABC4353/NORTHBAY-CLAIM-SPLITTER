from __future__ import annotations

import re

import fitz

BOX26 = fitz.Rect(190, 678, 245, 692)
_TOKEN = re.compile(r"\d{5,9}")


def page_runs(doc: fitz.Document) -> list[str | None]:
    out: list[str | None] = []
    for page in doc:
        words = page.get_text("words", clip=BOX26)
        tokens = [w[4] for w in words if _TOKEN.fullmatch(w[4])]
        out.append(tokens[0] if len(tokens) == 1 else None)
    return out


def format_run(token: str) -> str:
    return f"{token[:2]}-{token[2:]}"
