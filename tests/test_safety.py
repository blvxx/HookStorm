from __future__ import annotations

import pytest

from hookstorm.safety import (
    UnsafeTargetError,
    canonicalize_hostname,
    resolve_target,
    validate_target,
)


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


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:99999/webhook",
        "http://127.0.0.1:0/webhook",
        "https://example.com:99999/webhook",
        "http://localhost:not-a-port/webhook",
    ],
)
def test_rejects_invalid_ports_before_network_access(target: str) -> None:
    with pytest.raises(UnsafeTargetError, match="invalid hostname or port|between 1 and 65535"):
        validate_target(target, allow_remote=True, allow_insecure_http=True)


def test_rejects_malformed_ipv6_url() -> None:
    with pytest.raises(UnsafeTargetError, match="invalid hostname or port"):
        validate_target("http://[not-an-ipv6-address]/webhook")



@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("BÜCHER.DE", "xn--bcher-kva.de"),
        ("faß.de", "xn--fa-hia.de"),
        ("localhost.", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("2001:0db8::1", "2001:db8::1"),
    ],
)
def test_canonicalizes_hostnames_like_httpx(hostname: str, expected: str) -> None:
    assert canonicalize_hostname(hostname) == expected


def test_resolve_target_uses_ascii_idna_hostname_for_dns(monkeypatch) -> None:
    import socket

    calls: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", port))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolved = resolve_target("https://bücher.de/hook", allow_remote=True)

    assert calls == ["xn--bcher-kva.de"]
    assert resolved.hostname == "xn--bcher-kva.de"
    assert resolved.addresses == ("203.0.113.10",)
