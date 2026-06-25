"""Account fingerprinting — stable, format-insensitive, peppered, never raw."""
import pytest

from core.fingerprint import account_fingerprint


def test_format_insensitive_and_stable():
    a = account_fingerprint("1234-5678", salt="")
    b = account_fingerprint("1234 5678", salt="")
    c = account_fingerprint("12345678", salt="")
    assert a == b == c                 # punctuation/spacing don't matter
    assert len(a) == 64                # sha256 hex
    assert "12345678" not in a         # raw number never surfaces in the hash


def test_salt_changes_the_hash():
    assert account_fingerprint("12345678", salt="") != \
        account_fingerprint("12345678", salt="pepper")


def test_env_pepper_used_when_salt_unset(monkeypatch):
    monkeypatch.setenv("BANK_FINGERPRINT_SALT", "envpepper")
    assert account_fingerprint("12345678") == \
        account_fingerprint("12345678", salt="envpepper")


def test_blank_account_rejected():
    with pytest.raises(ValueError):
        account_fingerprint("N/A")
