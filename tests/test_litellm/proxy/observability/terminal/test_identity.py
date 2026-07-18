from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.identity import (
    MISSING_SESSION_BADGE,
    SessionAlias,
    SessionDigest,
    SessionHasher,
    new_event_id,
    new_worker_instance_id,
    resolve_session_aliases,
    session_badge,
)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _reference_crockford(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    width = (len(raw) * 8 + 4) // 5
    digits: tuple[str, ...] = ()
    for _ in range(width):
        value, remainder = divmod(value, 32)
        digits = (_CROCKFORD[remainder], *digits)
    return "".join(digits)


def test_session_hash_matches_independent_blake2s_crockford_vector() -> None:
    salt = bytes(range(32))
    session_id = "e96634a3-fa28-4083-b354-55542e2dca01"
    expected_digest = hashlib.blake2s(session_id.encode(), key=salt, digest_size=32).digest()
    expected_encoded = _reference_crockford(expected_digest)

    digest = SessionHasher(salt).digest(session_id)

    assert digest.raw == expected_digest
    assert digest.encoded == expected_encoded
    assert digest.encoded == "05A1N25N1VNKG9JG8YJ6S1EZPYBTR2BJS8FSZES993YCYY7QJ3NQ"
    assert digest.display(4) == expected_encoded[:4]


@pytest.mark.parametrize("salt", (b"", b"short", b"x" * 33))
def test_session_hasher_rejects_invalid_salt_length(salt: bytes) -> None:
    with pytest.raises(ValueError, match="16.*32"):
        SessionHasher(salt)


def test_alias_resolution_uses_four_characters_without_collision() -> None:
    first = SessionDigest(bytes.fromhex("00112233445566778899aabbccddeeff" * 2))
    second = SessionDigest(bytes.fromhex("10112233445566778899aabbccddeeff" * 2))

    aliases = resolve_session_aliases((first, second))

    assert aliases == (
        SessionAlias(first, first.encoded[:4]),
        SessionAlias(second, second.encoded[:4]),
    )


def test_alias_resolution_extends_all_colliding_prefixes() -> None:
    first = SessionDigest(b"\x08" + b"\x00" * 31)
    second = SessionDigest(b"\x08" + b"\x00" * 30 + b"\x01")
    assert first.encoded[:4] == second.encoded[:4]

    aliases = resolve_session_aliases((first, second))

    assert aliases[0].display_hash != aliases[1].display_hash
    assert len(aliases[0].display_hash) > 4
    assert len(aliases[1].display_hash) > 4
    assert first.encoded.startswith(aliases[0].display_hash)
    assert second.encoded.startswith(aliases[1].display_hash)


def test_alias_resolution_is_order_independent_and_deduplicates() -> None:
    first = SessionDigest(b"\x08" + b"\x00" * 31)
    second = SessionDigest(b"\x08" + b"\x00" * 30 + b"\x01")
    expected = resolve_session_aliases((first, second))
    actual = resolve_session_aliases((second, first, second))
    assert frozenset(actual) == frozenset(expected)


def test_alias_resolution_stops_at_first_unique_character() -> None:
    first = SessionDigest(bytes.fromhex("123400" + "00" * 29))
    second = SessionDigest(bytes.fromhex("123408" + "00" * 29))
    assert first.encoded[:4] == second.encoded[:4] == "04HM"

    aliases = resolve_session_aliases((first, second))

    assert tuple(alias.display_hash for alias in aliases) == ("04HM0", "04HM1")


def test_missing_and_present_session_badges() -> None:
    digest = SessionDigest(bytes.fromhex("abcdef0123456789" * 4))
    alias = SessionAlias(digest, digest.display(4))
    assert session_badge(None) == MISSING_SESSION_BADGE
    assert MISSING_SESSION_BADGE.symbol == "□"
    assert MISSING_SESSION_BADGE.display_hash == "----"
    assert session_badge(alias).symbol == "■"
    assert session_badge(alias).display_hash == alias.display_hash


def test_uuid_factories_use_injected_source() -> None:
    expected = UUID("00000000-0000-4000-8000-000000000123")
    source = lambda: expected
    assert new_event_id(source) is expected
    assert new_worker_instance_id(source) is expected
