"""tests/test_fingerprint.py — DA-5: FailureFingerprint unit tests."""

from __future__ import annotations

import pytest

from agent.tools import (
    FailureFingerprint,
    KNOWN_FINGERPRINTS,
    check_fingerprint,
    clear_fingerprints,
    get_seen_fingerprints,
    record_fingerprint,
)


@pytest.fixture(autouse=True)
def reset_fingerprints() -> None:
    clear_fingerprints()


# FP-01: key() format
def test_fp_key_default_pattern() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    assert fp.key() == "run_mypy:arg-type:*"


# FP-02: key() with explicit pattern
def test_fp_key_explicit_pattern() -> None:
    fp = FailureFingerprint(tool="write_repo_files", error_class="expected_dict", pattern="v2")
    assert fp.key() == "write_repo_files:expected_dict:v2"


# FP-03: frozen — no mutation
def test_fp_frozen() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    with pytest.raises(Exception):
        object.__setattr__(fp, "tool", "other")  # type: ignore[call-overload]


# FP-04: check_fingerprint returns False before record
def test_check_before_record() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    assert check_fingerprint(fp) is False


# FP-05: check_fingerprint returns True after record
def test_check_after_record() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    record_fingerprint(fp)
    assert check_fingerprint(fp) is True


# FP-06: different fingerprints are independent
def test_independent_fingerprints() -> None:
    fp1 = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    fp2 = FailureFingerprint(tool="write_repo_files", error_class="expected_dict")
    record_fingerprint(fp1)
    assert check_fingerprint(fp1) is True
    assert check_fingerprint(fp2) is False


# FP-07: clear_fingerprints resets state
def test_clear_resets() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    record_fingerprint(fp)
    clear_fingerprints()
    assert check_fingerprint(fp) is False


# FP-08: get_seen_fingerprints returns immutable snapshot
def test_get_seen_immutable() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    record_fingerprint(fp)
    seen = get_seen_fingerprints()
    assert fp.key() in seen
    with pytest.raises(Exception):
        seen.add("x")  # type: ignore[attr-defined]


# FP-09: get_seen_fingerprints empty before any record
def test_get_seen_empty_initially() -> None:
    assert get_seen_fingerprints() == frozenset()


# FP-10: KNOWN_FINGERPRINTS contains expected entries
def test_known_fingerprints_content() -> None:
    assert "mypy:arg-type:*" in KNOWN_FINGERPRINTS
    assert "write_repo_files:expected_dict:*" in KNOWN_FINGERPRINTS
    assert "CustomStreamWrapper:no_choices:*" in KNOWN_FINGERPRINTS


# FP-11: record same fingerprint twice — idempotent
def test_record_idempotent() -> None:
    fp = FailureFingerprint(tool="run_mypy", error_class="arg-type")
    record_fingerprint(fp)
    record_fingerprint(fp)
    assert len(get_seen_fingerprints()) == 1


# FP-12: pattern='*' is default
def test_pattern_default_is_star() -> None:
    fp = FailureFingerprint(tool="x", error_class="y")
    assert fp.pattern == "*"


# FP-13: two fingerprints same tool+error_class but different pattern are independent
def test_different_pattern_independent() -> None:
    fp_star = FailureFingerprint(tool="run_mypy", error_class="arg-type", pattern="*")
    fp_v2 = FailureFingerprint(tool="run_mypy", error_class="arg-type", pattern="v2")
    record_fingerprint(fp_star)
    assert check_fingerprint(fp_star) is True
    assert check_fingerprint(fp_v2) is False
