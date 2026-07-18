from __future__ import annotations

from dataclasses import dataclass

CREDENTIAL_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-litellm-api-key",
    }
)


@dataclass(frozen=True, slots=True)
class CapturedHeader:
    name: str
    value: str
    masked: bool


def capture_headers(headers: tuple[tuple[str, str], ...]) -> tuple[CapturedHeader, ...]:
    return tuple(
        CapturedHeader(
            name=name,
            value=_capture_value(name, value),
            masked=name.lower() in CREDENTIAL_HEADER_NAMES,
        )
        for name, value in headers
    )


def _capture_value(name: str, value: str) -> str:
    normalized_name = name.lower()
    if normalized_name not in CREDENTIAL_HEADER_NAMES:
        return value
    if normalized_name in {"authorization", "proxy-authorization"} and " " in value:
        scheme, credential = value.split(" ", 1)
        return f"{scheme} {mask_credential_value(credential)}"
    return mask_credential_value(value)


def mask_credential_value(value: str) -> str:
    if len(value) <= 1:
        return "…"
    if len(value) <= 8:
        return f"{value[0]}…{value[-1]}"
    return f"{value[:4]}…{value[-4:]}"
