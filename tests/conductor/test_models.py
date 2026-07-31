"""Domain model invariants for the conductor."""

from __future__ import annotations

from openjarvis.conductor.models import (
    ACTIVE_STATES,
    CONFLICT,
    LEASED,
    RUNNING,
    SUCCEEDED,
    Claim,
    ObservationFingerprint,
    RetryPolicy,
    make_owner_id,
)


def test_fingerprint_key_includes_every_identity_component():
    fp = ObservationFingerprint.from_turn("s1", 7, "2026-07-30T20:00:00Z", "oi")

    assert fp.key.startswith("s1:7:2026-07-30T20:00:00Z:")
    assert fp.content_hash in fp.key


def test_same_turn_content_yields_the_same_identity():
    a = ObservationFingerprint.from_turn("s1", 1, "t", "mesmo texto")
    b = ObservationFingerprint.from_turn("s1", 1, "t", "mesmo texto")

    assert a == b
    assert a.key == b.key


def test_empty_content_is_hashed_not_crashed():
    fp = ObservationFingerprint.from_turn("s1", 1, "t", "")

    assert len(fp.content_hash) == 64


def test_backoff_grows_then_caps():
    policy = RetryPolicy(backoff_seconds=60.0)

    assert policy.backoff_for(1) == 60.0
    assert policy.backoff_for(2) == 120.0
    assert policy.backoff_for(3) == 240.0
    assert policy.backoff_for(50) == policy.backoff_for(5)


def test_terminal_and_active_state_predicates():
    fp = ObservationFingerprint.from_turn("s1", 1, "t", "x")

    assert Claim(fingerprint=fp, state=SUCCEEDED).is_terminal()
    assert Claim(fingerprint=fp, state=CONFLICT).is_terminal()
    assert not Claim(fingerprint=fp, state=LEASED).is_terminal()
    assert ACTIVE_STATES == frozenset({LEASED, RUNNING})


def test_lease_is_only_active_before_it_expires():
    fp = ObservationFingerprint.from_turn("s1", 1, "t", "x")
    claim = Claim(fingerprint=fp, state=RUNNING, lease_expires_at=100.0)

    assert claim.lease_active_at(99.0) is True
    assert claim.lease_active_at(100.0) is False


def test_owner_id_identifies_this_process():
    owner = make_owner_id()

    assert owner.startswith("jarvis@")
    assert str(__import__("os").getpid()) in owner
