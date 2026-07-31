"""Integrity of the vendored vox-engine SDK.

The SDK is copied into ``_vendor/`` rather than imported from the extension
directory, so the backend keeps working when the owner moves or updates that
extension. A copy has one failure mode: it drifts, silently. Nobody edits a
vendored file on purpose, so a difference means either an accidental edit or a
copy that was refreshed without anyone looking at what changed.

Line endings are normalised before hashing: Git rewrites them on checkout, and a
CRLF/LF difference is not drift.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

VENDOR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "openjarvis"
    / "speech"
    / "_vendor"
)

#: sha256 of each vendored file, over LF-normalised bytes.
#: Regenerate deliberately when refreshing the SDK -- and read the diff first.
EXPECTED = {
    "_ed25519_ref.py": (
        "f527e89be8a4c9c95aad4b096f762afb39c64e9a645156946d7b2e2c11187bda"
    ),
    "vox_lifecycle.py": (
        "12e618b92a759118548717c43d6f1ed737f6bd238f5d7f917f84fd76f943860c"
    ),
    "vox_sdk.py": "d0249e8c0b6809901c91b58379436e86dc89319c551c3102df804143095349ad",
}


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_vendored_file_has_not_drifted(name):
    path = VENDOR / name
    assert path.exists(), f"{name} disappeared from _vendor/"
    assert _digest(path) == EXPECTED[name], (
        f"{name} differs from the vendored copy that was reviewed.\n"
        "Nobody edits a vendored file on purpose: either something was changed "
        "by accident, or the SDK was refreshed. If it was a refresh, read the "
        "diff and update EXPECTED in the same commit."
    )


def test_no_vendored_file_is_unaccounted_for():
    """A new file appearing here is drift too -- in the other direction."""
    present = {p.name for p in VENDOR.glob("*.py")} - {"__init__.py"}
    assert present == set(EXPECTED), (
        f"vendored set changed: {present ^ set(EXPECTED)}"
    )
