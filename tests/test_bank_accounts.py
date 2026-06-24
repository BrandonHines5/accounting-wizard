"""Tier 4 account registry: config loading, env-secret account numbers, and
per-account statement extraction with malformed-file resilience."""
import pandas as pd
import pytest

from bank.accounts import (BankAccount, extract_account, extract_statements,
                           load_bank_accounts)


def _ids(registry):
    return {e.id for e in registry}


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_load_bank_accounts(tmp_path):
    cfg = tmp_path / "bank_accounts.yaml"
    cfg.write_text(
        "accounts:\n"
        "  - entity_id: alpha\n"
        "    label: operating\n"
        "    format: csv\n"
        "    statement_glob: 'alpha/*.csv'\n"
        "    account_number_env: ALPHA_ACCT\n"
        "    columns:\n"
        "      amount: Amount\n")
    (a,) = load_bank_accounts(cfg)
    assert a.entity_id == "alpha" and a.fmt == "csv"
    assert a.statement_glob == "alpha/*.csv"
    assert a.columns == {"amount": "Amount"}


def test_account_number_read_from_env(monkeypatch):
    acct = BankAccount("alpha", "op", "ALPHA_ACCT", "alpha/*.csv")
    monkeypatch.delenv("ALPHA_ACCT", raising=False)
    with pytest.raises(ValueError):
        acct.account_number()                      # never invented, must be set
    monkeypatch.setenv("ALPHA_ACCT", "1234-5678")
    assert acct.account_number() == "1234-5678"


def test_extract_account_globs_and_extracts(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_ACCT", "12345678")
    _write_csv(tmp_path / "alpha" / "may.csv", [
        {"date": "2026-05-05", "description": "CHECK 1001", "amount": "-500.00", "check_no": "1001"},
        {"date": "2026-05-07", "description": "DEPOSIT", "amount": "2000.00", "check_no": ""},
    ])
    acct = BankAccount("alpha", "op", "ALPHA_ACCT", "alpha/*.csv")
    out = extract_account(acct, tmp_path, _ids(registry))
    assert len(out) == 2 and set(out["entity_id"]) == {"alpha"}


def test_extract_account_skips_malformed_file(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_ACCT", "12345678")
    _write_csv(tmp_path / "alpha" / "good.csv", [
        {"date": "2026-05-05", "description": "ok", "amount": "-500.00", "check_no": "1001"}])
    _write_csv(tmp_path / "alpha" / "bad.csv", [        # no amount/debit/credit column
        {"date": "2026-05-05", "description": "no amount", "check_no": "1002"}])
    errors = []
    acct = BankAccount("alpha", "op", "ALPHA_ACCT", "alpha/*.csv")
    out = extract_account(acct, tmp_path, _ids(registry),
                          on_error=lambda p, e: errors.append(p.name))
    assert len(out) == 1                              # only the good file survives
    assert errors == ["bad.csv"]


def test_extract_account_missing_secret_raises(registry, tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_ACCT", raising=False)
    acct = BankAccount("alpha", "op", "ALPHA_ACCT", "alpha/*.csv")
    with pytest.raises(ValueError):                   # config error → fail fast
        extract_account(acct, tmp_path, _ids(registry))


def test_extract_statements_concatenates_accounts(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_ACCT", "11111111")
    monkeypatch.setenv("BETA_ACCT", "22222222")
    _write_csv(tmp_path / "alpha" / "m.csv", [
        {"date": "2026-05-05", "description": "a", "amount": "-100.00", "check_no": ""}])
    _write_csv(tmp_path / "beta" / "m.csv", [
        {"date": "2026-05-06", "description": "b", "amount": "-200.00", "check_no": ""}])
    accounts = [BankAccount("alpha", "op", "ALPHA_ACCT", "alpha/*.csv"),
                BankAccount("beta", "op", "BETA_ACCT", "beta/*.csv")]
    out = extract_statements(accounts, tmp_path, _ids(registry))
    assert len(out) == 2 and set(out["entity_id"]) == {"alpha", "beta"}
