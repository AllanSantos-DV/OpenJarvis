"""Integrity of the vendored vox-engine SDK.

The SDK is copied into ``_vendor/`` rather than imported from the extension
directory, so the backend keeps working when the owner moves or updates that
extension. A copy has one failure mode: it drifts, silently.

The hashes live in ``vox_engine._VENDOR_SHA256`` and are enforced **at import**,
not only here -- a suite that catches this on someone's laptop does nothing for a
process that boots from a modified copy. These tests check the table is honest
and that the enforcement actually refuses.
"""

from __future__ import annotations

import hashlib

import pytest

from openjarvis.speech.vox_engine import (
    _VENDOR_DIR,
    _VENDOR_SHA256,
    VendoredSdkTampered,
    _verify_vendored,
)


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("name", sorted(_VENDOR_SHA256))
def test_vendored_file_matches_its_recorded_hash(name):
    path = _VENDOR_DIR / name
    assert path.exists(), f"{name} disappeared from _vendor/"
    assert _digest(path) == _VENDOR_SHA256[name]


def test_no_vendored_file_is_unaccounted_for():
    """A new file appearing here is drift too -- in the other direction."""
    present = {p.name for p in _VENDOR_DIR.glob("*.py")} - {"__init__.py"}
    assert present == set(_VENDOR_SHA256), (
        f"vendored set changed: {present ^ set(_VENDOR_SHA256)}"
    )


def test_a_modified_file_is_refused_loudly(tmp_path):
    """Loud on purpose: this code talks to a daemon holding a microphone."""
    fake = tmp_path / "vox_sdk.py"
    fake.write_text("# not the reviewed copy", encoding="utf-8")

    with pytest.raises(VendoredSdkTampered, match="difere da copia revisada"):
        _verify_vendored(fake)


def test_an_unknown_file_is_refused_too(tmp_path):
    """An unreviewed file is exactly what this exists to catch."""
    fake = tmp_path / "vox_surpresa.py"
    fake.write_text("# quem colocou isto aqui", encoding="utf-8")

    with pytest.raises(VendoredSdkTampered, match="nao esta na lista"):
        _verify_vendored(fake)


def test_line_endings_are_not_drift(tmp_path):
    """Git rewrites them on checkout; CRLF vs LF is not a modification."""
    original = (_VENDOR_DIR / "vox_sdk.py").read_bytes()
    crlf = tmp_path / "vox_sdk.py"
    crlf.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    _verify_vendored(crlf)  # must not raise
