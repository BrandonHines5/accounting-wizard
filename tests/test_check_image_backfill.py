"""Backfill scoping for statement-PDF check images: with `latest_only`, a
multi-month run extracts check images from only the most recent statement file
(so it reconciles every month but vision-reads just the latest month's checks)."""
import bank.statement_extract as se
from bank.accounts import BankAccount
from skill.run import _statement_pdf_check_source


def _account(**over):
    base = dict(entity_id="hines-homes", label="operating",
                account_number_env="HINES_HOMES_OPERATING_ACCT",
                statement_glob="hines-homes/operating/*.pdf", fmt="pdf",
                layout="first_service_bank",
                check_images={"source": "statement_pdf"})
    base.update(over)
    return BankAccount(**base)


def _seed(tmp_path, *names):
    d = tmp_path / "hines-homes" / "operating"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"%PDF-1.4")


def test_latest_only_reads_just_the_newest_statement(tmp_path, monkeypatch):
    # statement filenames sort chronologically (…_YYYYMMDD…), so newest sorts last
    _seed(tmp_path, "EStatement_0452_D_20250531.pdf",
          "EStatement_0452_D_20260430.pdf")
    seen = []
    monkeypatch.setattr(se, "extract_check_images",
                        lambda path, layout: (seen.append(__import__("pathlib").Path(path).name)
                                              or {"8001": b"img"}))
    src = _statement_pdf_check_source(
        _account(check_images={"source": "statement_pdf", "latest_only": True}), tmp_path)
    assert seen == ["EStatement_0452_D_20260430.pdf"]   # only the newest read
    assert src is not None and src.read_front("8001") == b"img"


def test_without_latest_only_reads_every_statement(tmp_path, monkeypatch):
    _seed(tmp_path, "EStatement_0452_D_20250531.pdf",
          "EStatement_0452_D_20260430.pdf")
    seen = []
    monkeypatch.setattr(se, "extract_check_images",
                        lambda path, layout: (seen.append(__import__("pathlib").Path(path).name)
                                              or {}))
    _statement_pdf_check_source(_account(), tmp_path)
    assert len(seen) == 2                                # both statements read
