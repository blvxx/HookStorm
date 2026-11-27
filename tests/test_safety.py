from __future__ import annotations

import pytest

from hookstorm.safety import UnsafeTargetError, validate_target


def test_accepts_loopback_targets() -> None:
    assert validate_target("http://127.0.0.1:8000/webhook") == "http://127.0.0.1:8000/webhook"
    assert validate_target("http://localhost:8000/webhook") == "http://localhost:8000/webhook"


@pytest.mark.parametrize(
    "target",
    ["https://example.com/hook", "http://192.168.1.20/hook", "file:///etc/passwd", "http://localhost.evil/hook"],
)
def test_blocks_nonlocal_or_invalid_targets_by_default(target: str) -> None:
    with pytest.raises(UnsafeTargetError):
        validate_target(target)


def test_remote_target_requires_explicit_opt_in() -> None:
    assert validate_target("https://example.com/hook", allow_remote=True) == "https://example.com/hook"


def test_remote_http_requires_explicit_insecure_transport_opt_in() -> None:
    with pytest.raises(UnsafeTargetError, match="unencrypted"):
        validate_target("http://example.com/hook", allow_remote=True)

    assert validate_target(
        "http://example.com/hook",
        allow_remote=True,
        allow_insecure_http=True,
    ) == "http://example.com/hook"


def test_loopback_http_does_not_require_insecure_transport_opt_in() -> None:
    assert (
        validate_target("http://127.0.0.1:8000/hook", allow_remote=True)
        == "http://127.0.0.1:8000/hook"
    )


def test_insecure_http_opt_in_requires_remote_opt_in() -> None:
    with pytest.raises(UnsafeTargetError, match="requires --allow-remote"):
        validate_target("http://example.com/hook", allow_insecure_http=True)


def test_rejects_embedded_credentials_even_when_remote_is_allowed() -> None:
    with pytest.raises(UnsafeTargetError, match="credentials"):
        validate_target("https://user:pass@example.com/hook", allow_remote=True)
