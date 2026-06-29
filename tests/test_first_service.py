"""First Service Bank positional PDF parser (Tier 4, T4-01).

The parser is pure — it consumes pdfplumber word boxes, not a PDF — so these tests
build *synthetic* word layouts that reproduce the statement's quirks (no real
financial data, per CLAUDE.md). Covered: the three sections, the 3-up checks grid,
a superscripted break-marker '*' that lands a fraction of a point off its row, a
split amount glyph ('7 91. 15' → 791.15), a sub-dollar '.25', the credit/debit sign
convention, statement-year derivation, and the Daily Balance Summary stop.
"""
import pandas as pd
import pytest

from bank.statement_extract import (extract_pdf, normalize_register,
                                    parse_first_service_words, PDF_LAYOUTS,
                                    _fsb_year_for, first_service_check_anchors)

# Column x0 positions copied from the real layout (see statement_extract geometry).
_CRED_DESC_X0, _DEBIT_DESC_X0 = 172, 188
# checks grid: (date, check, amount-right-edge) per column group
_G1, _G2, _G3 = 50, 234, 417


def _w(text, x0, top):
    return {"text": text, "x0": x0, "x1": x0 + len(text) * 5, "top": float(top)}


class Page:
    """Accumulate rows of (text, x0) into word boxes, one `top` per row (12pt apart)."""
    def __init__(self):
        self.words = []
        self._top = 0.0

    def row(self, *cells, dtops=None):
        self._top += 12.0
        dtops = dtops or {}
        for i, (text, x0) in enumerate(cells):
            self.words.append(_w(text, x0, self._top + dtops.get(i, 0.0)))
        return self


def _statement_pages():
    """A miniature two-'page' First Service Bank statement exercising every quirk."""
    p1 = Page()
    p1.row(("Statement", 50), ("Date:", 120), ("04-30-26", 200))
    p1.row(("Deposits", 50), ("and", 110), ("Other", 140), ("Credits", 180))
    p1.row(("Date", 50), ("Deposits", 110), ("Activity", 175), ("Description", 240))
    p1.row(("4/01", 50), ("52,338.43", 97), ("Buildertrend", _CRED_DESC_X0), ("Sol/PAYOUT", 249))
    p1.row(("TRN*1*4VXRFL6AIGZZNNEV", _CRED_DESC_X0))     # continuation — no date/amount
    p1.row(("4/30", 50), ("380.51", 130), ("INTEREST", _CRED_DESC_X0), ("EARNED", 235))

    p2 = Page()
    p2.row(("Other", 50), ("Debits", 90), ("and", 130), ("Withdrawals", 160))
    p2.row(("Date", 50), ("Withdrawals", 110), ("Activity", 175), ("Description", 240))
    p2.row(("4/01", 50), ("2,684.51", 110), ("Ln", _DEBIT_DESC_X0), ("pmnt", 210))
    p2.row(("4/02", 50), ("120.45", 120), ("Coney", _DEBIT_DESC_X0))
    p2.row(("CHECK", _DEBIT_DESC_X0), ("5065", 230))      # check-image ref — skipped
    p2.row(("4/16", 50), (".25", 150), ("PAYMENTUS/BILLPAY", _DEBIT_DESC_X0))   # sub-dollar
    p2.row(("4/14", 50), ("391.", 130), ("20", 153), ("Colonial", _DEBIT_DESC_X0))  # split glyph
    # Checks grid
    p2.row(("Checks", 50))
    p2.row(("Date", 50), ("Check", 90), ("No", 115), ("Amount", 150),
           ("Date", 234), ("Check", 274), ("No", 299), ("Amount", 334),
           ("Date", 417), ("Check", 457), ("No", 482), ("Amount", 517))
    # row 1: group-3 check number is superscripted ('8081*') — sits ~0.3pt high
    p2.row(("4/17", _G1), ("7166", 99), ("720.00", 162),
           ("4/15", _G2), ("8053", 283), ("574.76", 345),
           ("4/21", _G3), ("8081*", 467), ("400.00", 529),
           dtops={7: -0.3})
    # row 2: group-3 amount arrives split as '7' '91.' '15' → 791.15
    p2.row(("4/07", _G1), ("8034", 99), ("675.00", 162),
           ("4/09", _G2), ("8062", 283), ("1,500.00", 333),
           ("4/27", _G3), ("8091", 467), ("7", 529), ("91.", 535), ("15", 552))
    # row 3: only the first column is populated (odd final check)
    p2.row(("4/13", _G1), ("8052", 99), ("32,743.62", 150))
    p2.row(("*", 50), ("indicates", 60), ("a", 120), ("break", 130))   # legend — skipped
    p2.row(("Daily", 50), ("Balance", 90), ("Summary", 140))
    p2.row(("4/01", 50), ("250,000.00", 110), ("4/13", 234), ("112,449.90", 300))  # after stop

    return [p1.words, p2.words]


@pytest.fixture
def parsed():
    return parse_first_service_words(_statement_pages())


def test_section_totals_reconcile(parsed):
    credits = parsed[parsed["amount"] > 0]
    debits = parsed[parsed["amount"] < 0]
    assert len(credits) == 2
    assert round(credits["amount"].sum(), 2) == round(52338.43 + 380.51, 2)
    # 4 non-check debits + 7 checks = 11 disbursements (the Coney line is a real
    # debit; its 'CHECK 5065' continuation is only the cleared-check image ref)
    assert len(debits) == 11
    non_check = debits[debits["check_no"] == ""]
    assert len(non_check) == 4
    assert round(-non_check["amount"].sum(), 2) == round(2684.51 + 120.45 + 0.25 + 391.20, 2)


def test_amounts_are_signed(parsed):
    by_desc = parsed.set_index("description")["amount"]
    assert by_desc["INTEREST EARNED"] == 380.51        # credit → positive
    assert by_desc["Ln pmnt"] == -2684.51              # debit → negative
    assert by_desc["Check 7166"] == -720.00            # check → negative


def test_superscript_break_marker_check_is_captured(parsed):
    # Regression: the '*' sits ~0.3pt off the row; round(top/tol) bucketing would
    # split '8081*' onto a phantom line and drop the check. Gap clustering keeps it.
    checks = parsed.set_index("check_no")["amount"]
    assert checks["8081"] == -400.00                   # the '*' is stripped from the number
    assert "8081*" not in checks.index


def test_split_amount_glyph_is_reassembled(parsed):
    # '7' '91.' '15' across the amount band → 791.15, not three broken tokens.
    assert parsed.set_index("check_no").loc["8091", "amount"] == -791.15


def test_sub_dollar_amount_kept(parsed):
    assert parsed.set_index("description").loc["PAYMENTUS/BILLPAY", "amount"] == -0.25


def test_split_glyph_in_main_section(parsed):
    assert parsed.set_index("description").loc["Colonial", "amount"] == -391.20


def test_statement_year_is_applied(parsed):
    dates = pd.to_datetime(parsed["date"])
    assert (dates.dt.year == 2026).all()
    assert dates.min() == pd.Timestamp("2026-04-01")
    assert dates.max() == pd.Timestamp("2026-04-30")


def test_daily_balance_summary_rows_are_not_transactions(parsed):
    # The balance table is dated like a register but must not become transactions.
    assert 250000.00 not in parsed["amount"].abs().tolist()
    assert len(parsed) == 13                           # 2 credits + 4 debits + 7 checks


def test_year_wrap_for_prior_month():
    # A December line on a January statement belongs to the prior year.
    assert _fsb_year_for(month=12, stmt_month=1, stmt_year=2026) == 2025
    assert _fsb_year_for(month=1, stmt_month=1, stmt_year=2026) == 2026


def test_parser_output_feeds_normalize_register(registry, parsed):
    out = normalize_register(parsed, entity_id="alpha", account_number="123456789",
                             known_entity_ids={e.id for e in registry}, salt="")
    assert len(out) == 13
    # the raw account number is hashed, never present in any cell
    assert not out.astype("string").apply(
        lambda c: c.str.contains("123456789", na=False)).to_numpy().any()


def test_extract_pdf_unknown_layout_raises(registry, tmp_path):
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="Unknown PDF statement layout"):
        extract_pdf(f, entity_id="alpha", account_number="1",
                    known_entity_ids={e.id for e in registry}, layout="nope")


def test_legacy_layout_is_rejected():
    # The pre-2025-05 First Service layout ("Credits and Miscellaneous Debits")
    # must raise so extract_account logs it as skipped, not silently return nothing.
    p = Page()
    p.row(("Statement", 50), ("Date:", 120), ("11-30-24", 200))
    p.row(("Credits", 50), ("and", 110), ("Miscellaneous", 150), ("Debits", 240))
    p.row(("11/01", 50), ("30,805.50", 110), ("BUILDERTREND", 188))
    with pytest.raises(ValueError, match="legacy statement layout"):
        parse_first_service_words([p.words])


def test_check_image_anchors_match_caption_triples():
    # An image-page row with two columns: left has the usual date+check+amount
    # caption; right has check+amount with NO leading date (the 8106 case).
    row = Page()
    row.row(("04/28/2026", 78), ("8096", 138), ("$26,000.00", 175),
            ("8106", 410), ("$64.70", 470))
    anchors = first_service_check_anchors(row.words)
    by_check = {cn: (round(x0), round(top)) for cn, x0, top in anchors}
    assert set(by_check) == {"8096", "8106"}
    assert by_check["8096"][0] < 310          # left column
    assert by_check["8106"][0] >= 310         # right column (no date prefix needed)


def test_check_image_anchors_ignore_non_captions():
    # Garbled check-face text and a 9-digit account number must not look like a
    # caption (check numbers are 3–6 digits; the amount token must be clean).
    row = Page()
    row.row(("Account", 60), ("Number:", 110), ("123456789", 200), ("$720.00", 300))
    row.row(("Nineteen", 60), ("Thousand", 120), ("and", 200), ("20/100", 240))
    assert first_service_check_anchors(row.words) == []


def test_extract_pdf_dispatches_to_layout(registry, tmp_path, monkeypatch):
    # Dispatch + normalization without a real PDF: register a fake layout parser.
    frame = pd.DataFrame([{"date": "2026-04-01", "description": "d",
                           "amount": -10.0, "check_no": "1001"}])
    monkeypatch.setitem(PDF_LAYOUTS, "fake", lambda path: frame)
    f = tmp_path / "stmt.pdf"
    f.write_bytes(b"%PDF-1.4")
    out = extract_pdf(f, entity_id="alpha", account_number="999",
                      known_entity_ids={e.id for e in registry}, salt="", layout="fake")
    assert len(out) == 1 and out.iloc[0]["amount"] == -10.0
    assert out.iloc[0]["check_no"] == "1001"
