from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SESSION_DIGEST_BYTES = 32
_MIN_SALT_BYTES = 16
_MAX_SALT_BYTES = 32
_DEFAULT_ALIAS_LENGTH = 4

UUIDSource = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class SessionDigest:
    raw: bytes

    def __post_init__(self) -> None:
        if len(self.raw) != _SESSION_DIGEST_BYTES:
            raise ValueError(f"session digest must be {_SESSION_DIGEST_BYTES} bytes")

    @property
    def encoded(self) -> str:
        value = int.from_bytes(self.raw, "big")
        width = (len(self.raw) * 8 + 4) // 5
        digits: tuple[str, ...] = ()
        for _index in range(width):
            value, remainder = divmod(value, 32)
            digits = (_CROCKFORD_ALPHABET[remainder], *digits)
        return "".join(digits)

    def display(self, length: int) -> str:
        if length < 1 or length > len(self.encoded):
            raise ValueError("display length must fit the encoded digest")
        return self.encoded[:length]


@dataclass(frozen=True, slots=True)
class SessionAlias:
    digest: SessionDigest
    display_hash: str

    def __post_init__(self) -> None:
        if not self.display_hash or not self.digest.encoded.startswith(self.display_hash):
            raise ValueError("display_hash must be a non-empty prefix of the session digest")


@dataclass(frozen=True, slots=True)
class SessionBadge:
    symbol: str
    display_hash: str


MISSING_SESSION_BADGE = SessionBadge(symbol="□", display_hash="----")


@dataclass(frozen=True, slots=True)
class SessionHasher:
    salt: bytes

    def __post_init__(self) -> None:
        if not _MIN_SALT_BYTES <= len(self.salt) <= _MAX_SALT_BYTES:
            raise ValueError("session hash salt must contain 16 to 32 bytes")

    def digest(self, session_id: str) -> SessionDigest:
        if not session_id:
            raise ValueError("session_id must not be empty")
        return SessionDigest(
            hashlib.blake2s(session_id.encode("utf-8"), key=self.salt, digest_size=_SESSION_DIGEST_BYTES).digest()
        )


def resolve_session_aliases(digests: tuple[SessionDigest, ...]) -> tuple[SessionAlias, ...]:
    unique = tuple(sorted(frozenset(digests), key=lambda digest: digest.raw))
    encoded = tuple(digest.encoded for digest in unique)
    if len(frozenset(encoded)) != len(encoded):
        raise ValueError("different session digests have identical encoded values")
    return tuple(
        SessionAlias(digest, digest.display(_unique_prefix_length(digest.encoded, encoded))) for digest in unique
    )


def _unique_prefix_length(value: str, all_values: tuple[str, ...]) -> int:
    for length in range(_DEFAULT_ALIAS_LENGTH, len(value) + 1):
        prefix = value[:length]
        if sum(other.startswith(prefix) for other in all_values) == 1:
            return length
    raise ValueError("session digest cannot be distinguished from its peers")


def session_badge(alias: SessionAlias | None) -> SessionBadge:
    return MISSING_SESSION_BADGE if alias is None else SessionBadge(symbol="■", display_hash=alias.display_hash)


def new_event_id(source: UUIDSource = uuid4) -> UUID:
    return source()


def new_worker_instance_id(source: UUIDSource = uuid4) -> UUID:
    return source()
