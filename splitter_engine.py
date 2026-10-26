from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

import fitz

import config
from claims import format_run, page_runs
from invoice_pdf import InvoicePdfError, read_sections
from pbi_client import PbiClient, RunInfo

__version__ = "2.0.0"

logger = logging.getLogger(__name__)

MAX_INPUT_BYTES: int = 250 * 1024 * 1024
MAX_INPUT_PAGES: int = 5000

_CONTINUED_MARKER = "continued on next page"


class CollationError(ValueError):
    def __init__(self, message: str, items: list[str]):
        self.items = items
        super().__init__(message)


class EmptyPageError(ValueError):
    def __init__(self, page_number: int):
        self.page_number = page_number
        super().__init__(
            f"Page {page_number} has no extractable text — image-only PDF? Run OCR before splitting."
        )


class OutputExistsError(ValueError):
    def __init__(self, existing_paths: list[str]):
        self.existing_paths = existing_paths
        names = ", ".join(os.path.basename(p) for p in existing_paths)
        super().__init__(
            f"Output file(s) already exist: {names}. Pass overwrite=True to replace them."
        )


class InputTooLargeError(ValueError):
    def __init__(self, page_count: int, size_bytes: int):
        self.page_count = page_count
        self.size_bytes = size_bytes
        super().__init__(
            f"Input PDF is {page_count} pages / {size_bytes:,} bytes "
            f"(limit: {MAX_INPUT_PAGES} pages / {MAX_INPUT_BYTES:,} bytes). "
            "Pass override_size_limit=True to proceed anyway."
        )


class OutputCorruptError(ValueError):
    def __init__(self, output_path: str, expected_pages: int, actual_pages: int):
        self.output_path = output_path
        self.expected_pages = expected_pages
        self.actual_pages = actual_pages
        super().__init__(
            f"Saved {os.path.basename(output_path)} has {actual_pages} pages, "
            f"expected {expected_pages} — possible disk truncation."
        )


@dataclass
class CollateResult:
    input_path: str
    total_pages: int
    output_paths: list[str] = field(default_factory=list)
    invoices: dict[str, str] = field(default_factory=dict)
    invoice_pages: dict[str, int] = field(default_factory=dict)
    claim_pages: dict[str, int] = field(default_factory=dict)
    unprinted_runs: list[str] = field(default_factory=list)
    claims_missing: list[str] = field(default_factory=list)


def _output_path(input_path: str, suffix: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(input_path))
    stem, _ = os.path.splitext(os.path.basename(input_path))
    return os.path.join(base_dir, f"{stem}-{suffix}.pdf")


def _token_key(run: str) -> str:
    return run.replace("-", "")


def _leg_keys(infos: list[RunInfo]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for i in infos:
        trip_date = (i.trip_date or "").strip()
        base_job = (i.job or "").strip()[:-2]
        if trip_date and base_job:
            keys.add((trip_date, base_job))
    return keys


def _fill_continuations(src: fitz.Document, tokens: list[str | None]) -> list[str | None]:
    out = list(tokens)
    for idx in range(1, len(out)):
        if out[idx] is not None or out[idx - 1] is None:
            continue
        prev_text = (src[idx - 1].get_text() or "").lower()
        if _CONTINUED_MARKER in prev_text:
            out[idx] = out[idx - 1]
    return out


def _claims_in_order(tokens: list[str]) -> dict[str, list[int]]:
    claims: dict[str, list[int]] = {}
    for idx, token in enumerate(tokens):
        claims.setdefault(token, []).append(idx)
    return claims


def _order_claims(
    claim_tokens: list[str],
    claims: dict[str, list[int]],
    printed_runs: list[str],
    lookup: dict[str, list[RunInfo]],
) -> tuple[list[str], list[str], list[str]]:
    printed_pos: dict[str, int] = {}
    key_pos: dict[tuple[str, str], int] = {}
    for n, run in enumerate(printed_runs):
        token = _token_key(run)
        if token in printed_pos:
            continue
        printed_pos[token] = n
        for key in _leg_keys(lookup.get(token, [])):
            key_pos.setdefault(key, n)
    ranked: list[tuple[tuple[int, int, int], str]] = []
    unprinted: list[str] = []
    for token in sorted(claim_tokens, key=lambda t: claims[t][0]):
        first_page = claims[token][0]
        if token in printed_pos:
            ranked.append(((printed_pos[token], 0, first_page), token))
            continue
        positions = [key_pos[k] for k in _leg_keys(lookup.get(token, [])) if k in key_pos]
        if positions:
            ranked.append(((min(positions), 1, first_page), token))
        else:
            ranked.append(((len(printed_runs), 2, first_page), token))
            unprinted.append(format_run(token))
    ranked.sort()
    present = set(claim_tokens)
    missing: list[str] = []
    for run in printed_runs:
        if _token_key(run) not in present and run not in missing:
            missing.append(run)
    return [t for _, t in ranked], unprinted, missing


def _write_outputs(planned: list[tuple[str, list[tuple[fitz.Document, int]]]]) -> list[str]:
    written_tmps: list[str] = []
    committed: list[tuple[str, str | None]] = []
    finals: list[str] = []
    try:
        for final_path, parts in planned:
            tmp_path = final_path + ".tmp"
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            out_doc = fitz.open()
            try:
                for doc, page in parts:
                    out_doc.insert_pdf(doc, from_page=page, to_page=page)
                out_doc.save(tmp_path)
            finally:
                out_doc.close()
            written_tmps.append(tmp_path)
            with fitz.open(tmp_path) as verify:
                actual = len(verify)
            if actual != len(parts):
                raise OutputCorruptError(tmp_path, len(parts), actual)

        for final_path, parts in planned:
            tmp_path = final_path + ".tmp"
            bak_path: str | None = None
            if os.path.exists(final_path):
                bak_path = final_path + ".bak"
                if os.path.exists(bak_path):
                    os.unlink(bak_path)
                os.replace(final_path, bak_path)
            try:
                os.replace(tmp_path, final_path)
            except OSError:
                if bak_path is not None and os.path.exists(bak_path):
                    try:
                        os.replace(bak_path, final_path)
                    except OSError:
                        logger.warning(
                            "Failed to restore %s from %s during rollback",
                            os.path.basename(final_path),
                            os.path.basename(bak_path),
                        )
                raise
            committed.append((final_path, bak_path))
            finals.append(final_path)
            logger.info("Wrote %s (%d pages)", os.path.basename(final_path), len(parts))

        for _final, bak in committed:
            if bak is not None:
                try:
                    if os.path.exists(bak):
                        os.unlink(bak)
                except OSError:
                    logger.warning("Failed to remove backup %s", os.path.basename(bak))
    except Exception:
        for final_path, bak_path in committed:
            try:
                if bak_path is None:
                    if os.path.exists(final_path):
                        os.unlink(final_path)
                else:
                    os.replace(bak_path, final_path)
            except OSError:
                logger.warning("Rollback failed for %s", os.path.basename(final_path))
        for tmp in written_tmps:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                logger.warning("Failed to clean up tmp file %s", os.path.basename(tmp))
        finals.clear()
        raise
    return finals


def collate_pdf(
    input_path: str,
    *,
    client: PbiClient,
    on_status: Callable[[str], None],
    overwrite: bool = False,
    override_size_limit: bool = False,
) -> CollateResult:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(input_path)
    size_bytes = os.path.getsize(input_path)

    if not overwrite:
        existing = [
            p for p in (_output_path(input_path, s) for s in config.SUFFIX_ORDER) if os.path.exists(p)
        ]
        if existing:
            raise OutputExistsError(existing)

    src = fitz.open(input_path)
    try:
        if src.is_encrypted and src.needs_pass:
            raise ValueError(f"Input PDF is encrypted and requires a password: {input_path}")
        total_pages = len(src)
        if total_pages == 0:
            raise ValueError(f"Input PDF has 0 pages — empty or unreadable: {input_path}")
        if not override_size_limit and (size_bytes > MAX_INPUT_BYTES or total_pages > MAX_INPUT_PAGES):
            raise InputTooLargeError(page_count=total_pages, size_bytes=size_bytes)

        on_status("Reading run numbers from the claims...")
        for idx in range(total_pages):
            if not (src[idx].get_text() or "").strip():
                raise EmptyPageError(page_number=idx + 1)
        raw_tokens = _fill_continuations(src, page_runs(src))
        unreadable = [f"page {i + 1}" for i, t in enumerate(raw_tokens) if t is None]
        if unreadable:
            raise CollationError("These pages have no readable run number in box 26.", unreadable)
        tokens: list[str] = [t for t in raw_tokens if t is not None]

        on_status("Signing in to Power BI...")
        client.sign_in()
        client.refresh_invoicer(on_status)

        on_status("Looking up invoice numbers...")
        raw_lookup = client.invoice_lookup()
        lookup: dict[str, list[RunInfo]] = {}
        for key, infos in raw_lookup.items():
            valid = [i for i in infos if (i.invoice or "").strip()]
            if valid:
                lookup[key] = valid
        claims = _claims_in_order(tokens)
        unknown = [format_run(t) for t in claims if t not in lookup]
        if unknown:
            raise CollationError("These runs have no NorthBay invoice number.", unknown)
        conflicting = [
            f"{format_run(t)}: {', '.join(sorted({i.invoice.strip() for i in lookup[t]}))}"
            for t in claims if len({i.invoice.strip() for i in lookup[t]}) > 1
        ]
        if conflicting:
            raise CollationError("These runs are on more than one invoice.", conflicting)

        invoice_of = {t: lookup[t][0].invoice.strip() for t in claims}
        by_suffix: dict[str, set[str]] = {}
        for inv in invoice_of.values():
            by_suffix.setdefault(inv[8:], set()).add(inv)
        odd = sorted(
            inv for s, invs in by_suffix.items() if s not in config.SUFFIX_ORDER for inv in invs
        )
        if odd:
            raise CollationError("These invoice numbers have an unknown suffix.", odd)
        multi = [
            f"{s}: {', '.join(sorted(by_suffix[s]))}"
            for s in config.SUFFIX_ORDER if s in by_suffix and len(by_suffix[s]) > 1
        ]
        if multi:
            raise CollationError(
                "This PDF has claims from more than one invoice in the same group.", multi
            )
        invoices = {s: next(iter(by_suffix[s])) for s in config.SUFFIX_ORDER if s in by_suffix}

        if not overwrite:
            existing = [
                p for p in (_output_path(input_path, s) for s in invoices) if os.path.exists(p)
            ]
            if existing:
                raise OutputExistsError(existing)

        on_status("Downloading the invoices from Power BI...")
        pdf_bytes = client.export_invoices(list(invoices.values()))
        inv_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            try:
                sections = read_sections(inv_doc, list(invoices.values()))
            except InvoicePdfError as exc:
                raise CollationError(str(exc), exc.items) from exc

            result = CollateResult(input_path=input_path, total_pages=total_pages, invoices=invoices)
            planned: list[tuple[str, list[tuple[fitz.Document, int]]]] = []
            for suffix, invoice in invoices.items():
                section = sections[invoice]
                suffix_tokens = [t for t in claims if invoice_of[t] == invoice]
                ordered, unprinted, missing = _order_claims(suffix_tokens, claims, section.runs, lookup)
                parts = [(inv_doc, p) for p in section.pages]
                parts += [(src, p) for t in ordered for p in claims[t]]
                planned.append((_output_path(input_path, suffix), parts))
                result.invoice_pages[suffix] = len(section.pages)
                result.claim_pages[suffix] = len(parts) - len(section.pages)
                result.unprinted_runs += unprinted
                result.claims_missing += missing

            on_status("Writing the files...")
            result.output_paths = _write_outputs(planned)
            return result
        finally:
            inv_doc.close()
    finally:
        src.close()
