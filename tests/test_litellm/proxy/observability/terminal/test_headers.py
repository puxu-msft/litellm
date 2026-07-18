from __future__ import annotations

import pytest

from litellm.proxy.observability.terminal.capture.headers import (
    CREDENTIAL_HEADER_NAMES,
    CapturedHeader,
    capture_headers,
    mask_credential_value,
)


@pytest.mark.parametrize(
    ("name", "value", "masked"),
    (
        ("authorization", "Bearer github_pat_1234567890ABCDEFGH", "Bearer gith…EFGH"),
        ("authorization", "token_nospace_abcdef", "toke…cdef"),
        ("X-API-Key", "abcdefghijklmnop", "abcd…mnop"),
        ("x-litellm-api-key", "short", "s…t"),
        ("cookie", "session=abcdefghijklmnop", "sess…mnop"),
        ("set-cookie", "session=abcdefghijklmnop", "sess…mnop"),
        ("proxy-authorization", "Basic abcdefghijklmnop", "Basic abcd…mnop"),
    ),
)
def test_fixed_credential_headers_are_masked_case_insensitively(
    name: str,
    value: str,
    masked: str,
) -> None:
    assert name.lower() in CREDENTIAL_HEADER_NAMES
    assert capture_headers(((name, value),)) == (CapturedHeader(name=name, value=masked, masked=True),)
    assert value not in repr(capture_headers(((name, value),)))


def test_non_credential_headers_remain_verbatim() -> None:
    headers = (
        ("content-type", "application/json"),
        ("x-claude-code-session-id", "session-12345678"),
        ("x-custom-token", "not-masked-because-the-set-is-fixed"),
    )
    assert capture_headers(headers) == tuple(
        CapturedHeader(name=name, value=value, masked=False) for name, value in headers
    )


def test_header_order_and_duplicates_are_preserved() -> None:
    headers = (("x-api-key", "abcdefghij"), ("X-API-Key", "1234567890"), ("accept", "text/plain"))
    captured = capture_headers(headers)
    assert tuple(header.name for header in captured) == ("x-api-key", "X-API-Key", "accept")
    assert tuple(header.value for header in captured) == ("abcd…ghij", "1234…7890", "text/plain")


@pytest.mark.parametrize(
    ("value", "expected"),
    (("", "…"), ("a", "…"), ("ab", "a…b"), ("12345678", "1…8"), ("123456789", "1234…6789")),
)
def test_mask_credential_value_boundaries(value: str, expected: str) -> None:
    assert mask_credential_value(value) == expected
