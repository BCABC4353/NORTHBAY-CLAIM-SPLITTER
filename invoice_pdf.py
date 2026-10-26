from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

_INVOICE = re.compile(r"\d{8}[A-Z0-9]+")
_RUN = re.compile(r"\d{2}-\d+")
RUN_X_MIN = 70
RUN_X_MAX = 125


class InvoicePdfError(ValueError):
    def __init__(self, message: str, items: list[str]):
        self.items = items
        super().__init__(message)


@dataclass
class InvoiceSection:
    invoice: str
    pages: list[int] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)


def _invoice_on_page(words) -> str | None:
    for w in words:
        if w[4] != "INVOICE:":
            continue
        right = [
            v for v in words
            if abs(v[1] - w[1]) < 3 and v[0] > w[2] - 1 and _INVOICE.fullmatch(v[4])
        ]
        if right:
            return min(right, key=lambda v: v[0])[4]
    return None


def read_sections(doc: fitz.Document, expected: list[str]) -> dict[str, InvoiceSection]:
    sections: list[InvoiceSection] = []
    for i, page in enumerate(doc):
        words = page.get_text("words")
        invoice = _invoice_on_page(words)
        if invoice:
            sections.append(InvoiceSection(invoice=invoice))
        elif not sections:
            raise InvoicePdfError(
                "The exported invoices do not start with an invoice page.", ["page 1"]
            )
        current = sections[-1]
        current.pages.append(i)
        runs = sorted(
            (w[1], w[0], w[4]) for w in words
            if _RUN.fullmatch(w[4]) and RUN_X_MIN <= w[0] <= RUN_X_MAX
        )
        current.runs.extend(r[2] for r in runs)

    found = [s.invoice for s in sections]
    problems = [f"{inv} appears {found.count(inv)} times" for inv in sorted(set(found)) if found.count(inv) > 1]
    problems += [f"{inv} is missing from the export" for inv in expected if inv not in found]
    problems += [f"{inv} was not requested" for inv in sorted(set(found)) if inv not in expected]
    if problems:
        raise InvoicePdfError("The exported invoices do not match the claims.", problems)
    return {s.invoice: s for s in sections}
