"""Tier 4 check-image vision reads (T4-03/04/05).

Independent verification at the document level: what is physically printed and
endorsed on the cancelled check, read straight off the image and compared to what
the books say. This catches alterations the statement register can't show — a
payee that doesn't match the recorded vendor, an amount raised after signing, a
business check endorsed to an individual.

- T4-03 — read payee ≠ recorded vendor for that check number (CRITICAL); an
  image too blurry to read confidently (read_confidence < threshold) is routed to
  the human review queue (MEDIUM) instead of asserting a false match.
- T4-04 — read amount ≠ recorded amount (CRITICAL, possible alteration).
- T4-05 — endorsement anomalies on the back image: a business payee endorsed by
  an individual, or a double endorsement (HIGH).

Image handling (CLAUDE.md hard rule): images live in SharePoint. The caller
supplies `fetch_front`/`fetch_back` to pull bytes at runtime; this module reads
them, fills `payee_read`/`amount_read`/`read_confidence` on the bank rows, and
returns findings. It NEVER stores the image — only the reads and the path
reference already on the row.

The vision call is isolated behind `CheckReader`; `AnthropicCheckReader` is the
Claude implementation (optional dependency, imported lazily). Everything else —
the read→books comparison and rule logic — is deterministic and tested with an
injected fake reader.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from core.config import RulesConfig
from core.entities import EntityRegistry
from core.findings import Finding, Severity

MODEL = "claude-opus-4-8"

# Generic tokens that carry no identifying signal in a payee/vendor name.
_GENERIC_NAME_TOKENS = {"the", "llc", "inc", "co", "company", "corp", "corporation",
                        "ltd", "group", "and"}


@dataclass
class CheckRead:
    """Structured read of one cancelled-check image."""
    payee: str = ""
    amount: float | None = None
    date: str | None = None
    confidence: float = 0.0                     # 0–100; < threshold → review queue
    endorsement: str = ""                       # free-text back-image read
    endorsement_flags: tuple[str, ...] = ()     # e.g. 'double_endorsement'


class CheckReader:
    """Reads a cancelled-check image into a CheckRead. Inject a fake in tests."""

    def read_check(self, *, front: bytes, back: bytes | None = None,
                   media_type: str = "image/jpeg") -> CheckRead:
        raise NotImplementedError


def _name_tokens(name) -> set[str]:
    tokens = re.sub(r"[^a-z0-9 ]", " ", str(name).lower()).split()
    return {t for t in tokens if t} - _GENERIC_NAME_TOKENS


def _names_disagree(read_payee, book_vendor) -> bool:
    """True only when both names carry signal AND they substantially differ, so a
    blank read or a 'Acme Lumber Co' vs 'Acme Lumber' nuance never trips T4-03."""
    a, b = _name_tokens(read_payee), _name_tokens(book_vendor)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) < 0.5


def _norm_check(value) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _bank_ref(row) -> str:
    """Stable natural key for a check-image finding (see bank.reconcile._bank_ref)."""
    return "|".join([str(row.get("account_fingerprint") or ""), str(row.get("image_ref") or ""),
                     _norm_check(row.get("check_no"))])


def _is_cancelled_check(row) -> bool:
    return bool(_norm_check(row.get("check_no"))) and pd.notna(row.get("image_ref"))


def verify_check_images(
    bank: pd.DataFrame,
    transactions: pd.DataFrame,
    reader: CheckReader,
    registry: EntityRegistry,
    config: RulesConfig,
    *,
    fetch_front: Callable[[str], bytes],
    fetch_back: Callable[[pd.Series], bytes | None] | None = None,
    media_type: str = "image/jpeg",
    register_label: str | None = None,
) -> tuple[pd.DataFrame, list[Finding]]:
    """Read every cancelled-check image and compare it to the books.

    Returns the bank frame enriched with payee_read/amount_read/read_confidence,
    plus the T4-03/04/05 findings. `fetch_front(image_ref) -> bytes` pulls the
    image (e.g. from SharePoint); `fetch_back(row) -> bytes | None` optionally
    supplies the endorsement side for the rows you choose to inspect.
    `register_label` names the account these images belong to (e.g. '…0452', 'Ozk')
    so each finding tells the reviewer which register to search."""
    amount_tol = float(config.param("bank_amount_tolerance"))
    min_conf = float(config.param("check_image_min_confidence"))
    active = {e.id for e in registry.active()}

    bank = bank.copy()
    findings: list[Finding] = []
    for idx, row in bank.iterrows():
        if row["entity_id"] not in active or not _is_cancelled_check(row):
            continue
        front = fetch_front(row["image_ref"])
        back = fetch_back(row) if fetch_back is not None else None
        read = reader.read_check(front=front, back=back, media_type=media_type)

        bank.at[idx, "payee_read"] = read.payee
        bank.at[idx, "amount_read"] = read.amount
        bank.at[idx, "read_confidence"] = read.confidence

        findings.extend(_review_one(row, read, transactions, amount_tol, min_conf,
                                    register_label))

    findings.sort(key=lambda f: (-int(f.severity), f.rule_id))
    return bank, findings


# A cancelled check corresponds to a payment, never a bill/credit — so we only ever
# match its number against payment rows. This is belt-and-suspenders with the ingest
# split (which keeps a bill's invoice number out of check_no): even if a document
# number leaked into check_no, a bill can't be mistaken for the recorded check.
_CHECK_BOOK_TYPES = {"check", "bill_payment"}


def _book_check(transactions: pd.DataFrame, entity_id: str, check_no: str):
    """The recorded book check (a payment) for this entity + check number, if any."""
    same = transactions[(transactions["entity_id"] == entity_id)
                        & transactions["txn_type"].isin(_CHECK_BOOK_TYPES)
                        & (transactions["check_no"].map(_norm_check) == check_no)]
    return None if same.empty else same.iloc[0]


def _reg_detail(label: str | None) -> dict:
    """The `register` detail (a to_row() workbook column) when a label is known."""
    return {"register": label} if label else {}


def _reg_tag(label: str | None) -> str:
    """Inline register tag appended to a finding's question for at-a-glance reading."""
    return f" [Register: {label}]" if label else ""


def _review_one(row, read: CheckRead, transactions, amount_tol, min_conf,
                register_label: str | None = None) -> list[Finding]:
    entity_id = row["entity_id"]
    check_no = _norm_check(row["check_no"])
    book = _book_check(transactions, entity_id, check_no)
    refs = [str(book["source_id"])] if book is not None else []
    cleared = str(row["date"].date()) if pd.notna(row.get("date")) else None
    reg, tag = _reg_detail(register_label), _reg_tag(register_label)
    out: list[Finding] = []

    # Unreadable image → review queue, never a false assertion of (mis)match.
    if read.confidence < min_conf:
        out.append(Finding(
            "T4-03", Severity.MEDIUM, [entity_id],
            question=(f"Check #{check_no}'s image could not be read confidently "
                      f"(confidence {read.confidence:.0f}% < {min_conf:.0f}%). Pull the image "
                      "and confirm the payee and amount manually." + tag),
            details={"check_no": check_no, "read_confidence": float(read.confidence),
                     "image_review": "low_confidence", "bank_ref": _bank_ref(row),
                     "cleared_date": cleared, **reg},
            transactions=refs))
        return out

    if book is not None:
        if pd.notna(book.get("vendor_name")) and _names_disagree(read.payee, book["vendor_name"]):
            out.append(Finding(
                "T4-03", Severity.CRITICAL, [entity_id],
                question=(f"Check #{check_no} is payable to '{read.payee}' on the image but the "
                          f"books record vendor '{book['vendor_name']}'. Who was actually paid?"
                          + tag),
                details={"check_no": check_no, "read_payee": read.payee,
                         "recorded_vendor": book["vendor_name"], "bank_ref": _bank_ref(row),
                         "cleared_date": cleared, **reg},
                transactions=refs))
        recorded = abs(float(book["amount"]))
        if read.amount is not None and abs(float(read.amount) - recorded) > amount_tol:
            out.append(Finding(
                "T4-04", Severity.CRITICAL, [entity_id],
                question=(f"Check #{check_no} reads ${float(read.amount):,.2f} on the image but the "
                          f"books record ${recorded:,.2f}. Was the check altered after signing?"
                          + tag),
                details={"check_no": check_no, "read_amount": float(read.amount),
                         "recorded": recorded, "source": "check_image",
                         "bank_ref": _bank_ref(row), "cleared_date": cleared, **reg},
                transactions=refs))

    if read.endorsement_flags:
        out.append(Finding(
            "T4-05", Severity.HIGH, [entity_id],
            question=(f"Check #{check_no}'s endorsement looks irregular "
                      f"({', '.join(read.endorsement_flags)}). Confirm who deposited it "
                      "and that the endorsement is authorized." + tag),
            details={"check_no": check_no, "endorsement": read.endorsement,
                     "endorsement_flags": list(read.endorsement_flags),
                     "bank_ref": _bank_ref(row), "cleared_date": cleared, **reg},
            transactions=refs))
    return out


# --- Claude-backed reader (optional dependency) ---------------------------------

SYSTEM_PROMPT = """\
You read cancelled-check images for a forensic-accounting review. Transcribe only \
what is visibly printed or written — never guess. Report a calibrated confidence \
(0–100): high only when the payee line and courtesy/legal amount are clearly \
legible and agree. If a back (endorsement) image is provided, read the \
endorsement and flag anomalies: a business payee endorsed in an individual's \
name ('individual_endorsement_on_business_payee'), or more than one endorsement \
('double_endorsement')."""

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "payee": {"type": "string"},
        "amount": {"type": ["number", "null"]},
        "date": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "endorsement": {"type": "string"},
        "endorsement_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["payee", "amount", "date", "confidence",
                 "endorsement", "endorsement_flags"],
}

READ_INSTRUCTION = ("Read this check. Return payee, amount (number), date, your "
                    "confidence (0–100), and any endorsement reading/flags.")


class AnthropicCheckReader(CheckReader):
    """CheckReader backed by a Claude vision model, one structured call per check."""

    def __init__(self, client=None, model: str = MODEL, max_tokens: int = 800):
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic  # lazy: optional dependency
            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def _image_block(data: bytes, media_type: str) -> dict:
        b64 = base64.standard_b64encode(data).decode("ascii")
        return {"type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64}}

    def read_check(self, *, front: bytes, back: bytes | None = None,
                   media_type: str = "image/jpeg") -> CheckRead:
        content: list = [self._image_block(front, media_type)]
        if back is not None:
            content.append({"type": "text",
                            "text": "Above is the front; below is the back (endorsement side)."})
            content.append(self._image_block(back, media_type))
        content.append({"type": "text", "text": READ_INSTRUCTION})
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        return _parse_read(response)


def _parse_read(response) -> CheckRead:
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    if not text.strip():
        raise ValueError("Check-image response carried no text block to parse")
    data = json.loads(text)
    confidence = max(0.0, min(100.0, float(data.get("confidence", 0.0))))
    return CheckRead(
        payee=data.get("payee", ""),
        amount=data.get("amount"),
        date=data.get("date"),
        confidence=confidence,
        endorsement=data.get("endorsement", ""),
        endorsement_flags=tuple(data.get("endorsement_flags", []) or ()),
    )
