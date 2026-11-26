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


def test_rejects_embedded_credentials_even_when_remote_is_allowed() -> None:
    with pytest.raises(UnsafeTargetError, match="credentials"):
        validate_target("https://user:pass@example.com/hook", allow_remote=True)
