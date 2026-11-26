from __future__ import annotations

import base64
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from hookstorm.cli import main
from hookstorm.doctor import ConnectivityResult, _check_connectivity_detailed, run_preflight


def _scenario_file(
    tmp_path: Path,
    *,
    signature: str = "valid",
    provider: str = "generic",
) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        f"""version: 1
name: Doctor smoke
delivery:
  signature: {signature}
  signature_provider: {provider}
events:
  - id: ping
    event_id: evt_doctor_001
    payload:
      ok: true
""",
        encoding="utf-8",
    )
    return path


def _check(checks, label: str, status: str | None = None):
    matches = [check for check in checks if check.label == label]
    if status is not None:
        matches = [check for check in matches if check.status == status]
    assert matches, f"Missing {status or ''} check for {label}: {checks}"
    return matches[0]


def test_preflight_checks_config_without_contacting_network(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setattr(
        "hookstorm.doctor._check_connectivity",
        lambda *_args: pytest.fail("network helper must not run without --connect"),
    )

    checks = run_preflight(
        scenario,
        environment={
            "HOOKSTORM_TARGET_URL": "https://staging.example.test/webhook?token=private",
            "HOOKSTORM_SECRET": "private-secret",
        },
    )

    assert _check(checks, "Scenario", "PASS").message == "Doctor smoke (1 event(s))"
    assert _check(checks, "Target", "PASS").message.endswith("full URL hidden")
    assert _check(checks, "Target URL", "WARN")
    assert _check(checks, "Signing secret", "PASS").message == (
        "HOOKSTORM_SECRET is set (value hidden)"
    )
    assert _check(checks, "Network", "INFO")
    assert "private" not in " ".join(check.message for check in checks)


def _verification_scenario_file(
    tmp_path: Path,
    *,
    url: str = "https://staging.example.test/__hookstorm/state",
    auth: str = """  auth:
    type: bearer
    token_env: HOOKSTORM_VERIFY_TOKEN
""",
) -> Path:
    path = _scenario_file(tmp_path, signature="none")
    content = path.read_text(encoding="utf-8")
    content += (
        "verify:\n"
        f"  url: {url}\n"
        "  expect_status: [200]\n"
        "  expect_json: { processed_events: 1 }\n"
        + auth
    )
    path.write_text(content, encoding="utf-8")
    return path


def test_doctor_checks_bearer_auth_without_sending_credentials(tmp_path) -> None:
    scenario = _verification_scenario_file(tmp_path)
    secret = "not-for-output-token"
    checks = run_preflight(
        scenario,
        target="https://staging.example.test/webhook",
        environment={
            "HOOKSTORM_VERIFY_TOKEN": secret,
        },
    )

    check = _check(checks, "Verification auth", "PASS")
    assert "HOOKSTORM_VERIFY_TOKEN is set and valid" in check.message
    assert "value hidden" in check.message
    assert "no HTTP request was sent" in " ".join(check.details)
    assert "does not verify whether the endpoint accepts" in " ".join(check.details)
    assert secret not in str(checks)


def test_doctor_fails_when_verification_credential_is_missing(tmp_path) -> None:
    scenario = _verification_scenario_file(tmp_path)
    checks = run_preflight(
        scenario,
        target="https://staging.example.test/webhook",
        environment={},
    )

    check = _check(checks, "Verification auth", "FAIL")
    assert "HOOKSTORM_VERIFY_TOKEN" in check.message


@pytest.mark.parametrize(
    "credential",
    ["", "contains whitespace", "токен", "x" * 8_193],
)
def test_doctor_rejects_malformed_verification_credential(
    tmp_path, credential: str
) -> None:
    scenario = _verification_scenario_file(tmp_path)
    checks = run_preflight(
        scenario,
        target="https://staging.example.test/webhook",
        environment={"HOOKSTORM_VERIFY_TOKEN": credential},
    )

    assert _check(checks, "Verification auth", "FAIL")
    if credential:
        assert credential not in str(checks)


def test_doctor_checks_api_key_environment_variable(tmp_path) -> None:
    scenario = _verification_scenario_file(
        tmp_path,
        auth="""  auth:
    type: api_key
    key_env: HOOKSTORM_VERIFY_API_KEY
    header: X-Client-Key
""",
    )
    secret = "api-key-hidden"
    checks = run_preflight(
        scenario,
        target="https://staging.example.test/webhook",
        environment={"HOOKSTORM_VERIFY_API_KEY": secret},
    )

    check = _check(checks, "Verification auth", "PASS")
    assert "HOOKSTORM_VERIFY_API_KEY is set and valid" in check.message
    assert secret not in str(checks)


def test_doctor_rejects_remote_http_for_authenticated_verification(tmp_path) -> None:
    scenario = _verification_scenario_file(
        tmp_path,
        url="http://staging.example.test/__hookstorm/state",
    )
    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_VERIFY_TOKEN": "safe-test-token"},
    )

    check = _check(checks, "Verification auth", "FAIL")
    assert "require HTTPS" in check.message


def test_doctor_allows_http_for_loopback_authenticated_verification(tmp_path) -> None:
    scenario = _verification_scenario_file(
        tmp_path,
        url="http://127.0.0.1:8000/__hookstorm/state",
    )
    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_VERIFY_TOKEN": "safe-test-token"},
    )

    assert _check(checks, "Verification auth", "PASS")


def test_doctor_reports_when_verification_auth_is_not_configured(tmp_path) -> None:
    scenario = _verification_scenario_file(tmp_path, auth="")
    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={},
    )

    check = _check(checks, "Verification auth", "PASS")
    assert "unauthenticated" in check.message


def test_doctor_validates_standard_webhooks_secret(tmp_path) -> None:
    scenario = _scenario_file(tmp_path, signature="valid", provider="standard_webhooks")
    key = b"hookstorm-test-standard-webhooks-secret"
    secret = "whsec_" + base64.b64encode(key).decode("ascii")

    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_SECRET": secret},
    )

    check = _check(checks, "Signing secret", "PASS")
    assert "valid Standard Webhooks secret" in check.message
    assert secret not in str(checks)


@pytest.mark.parametrize("secret", ["plain-secret", "whsec_!!!!", "whsec_YQ=="])
def test_doctor_rejects_malformed_standard_webhooks_secret(tmp_path, secret: str) -> None:
    scenario = _scenario_file(tmp_path, signature="valid", provider="standard_webhooks")

    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_SECRET": secret},
    )

    check = _check(checks, "Signing secret", "FAIL")
    assert "Standard Webhooks" in check.message
    assert secret not in str(checks)


def test_preflight_fails_when_target_or_required_secret_is_missing(tmp_path) -> None:
    scenario = _scenario_file(tmp_path)
    checks = run_preflight(scenario, environment={})

    assert _check(checks, "Target", "FAIL")
    assert _check(checks, "Signing secret", "FAIL")
    assert _check(checks, "Scenario", "PASS")


def test_preflight_uses_event_level_signature_overrides(tmp_path) -> None:
    path = _scenario_file(tmp_path, signature="none")
    content = path.read_text(encoding="utf-8").replace(
        "    payload:\n      ok: true",
        "    delivery:\n      signature: valid\n    payload:\n      ok: true",
    )
    path.write_text(content, encoding="utf-8")
    checks = run_preflight(
        path,
        target="http://127.0.0.1:8000/webhook",
        environment={},
    )

    assert _check(checks, "Signing secret", "FAIL")


def test_preflight_does_not_require_secret_for_unsigned_scenario(tmp_path) -> None:
    scenario = _scenario_file(tmp_path, signature="none")
    checks = run_preflight(
        scenario,
        environment={"HOOKSTORM_TARGET_URL": "http://127.0.0.1:8000/webhook"},
    )

    assert _check(checks, "Signing secret", "PASS").message.startswith("not required")


@pytest.mark.parametrize(
    "target",
    [
        "ftp://staging.example.test/webhook",
        "https://user:password@staging.example.test/webhook",
        "https://staging.example.test:70000/webhook",
        "https://staging.example.test:0/webhook",
        "https://staging.example.test/webhook#fragment",
    ],
)
def test_preflight_rejects_unsafe_or_invalid_target_urls(tmp_path, target: str) -> None:
    scenario = _scenario_file(tmp_path)
    checks = run_preflight(
        scenario,
        target=target,
        environment={"HOOKSTORM_SECRET": "private-secret"},
    )

    assert _check(checks, "Target", "FAIL")
    assert _check(checks, "Network", "INFO")


def test_preflight_runs_socket_check_only_when_requested(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)
    captured = {}

    def fake_connect(parsed, timeout):
        captured["host"] = parsed.hostname
        captured["scheme"] = parsed.scheme
        captured["timeout"] = timeout
        return "TCP connection and TLS certificate verification succeeded"

    monkeypatch.setattr("hookstorm.doctor._check_connectivity", fake_connect)
    checks = run_preflight(
        scenario,
        target="https://staging.example.test:9443/webhook",
        environment={"HOOKSTORM_SECRET": "private-secret"},
        connect=True,
        timeout=7.5,
    )

    assert captured == {
        "host": "staging.example.test",
        "scheme": "https",
        "timeout": 7.5,
    }
    assert _check(checks, "Network", "PASS").message.startswith("TCP connection")


def test_preflight_reports_socket_errors(tmp_path, monkeypatch) -> None:
    scenario = _scenario_file(tmp_path)

    def fail_connect(*_args):
        raise TimeoutError("timed out")

    monkeypatch.setattr("hookstorm.doctor._check_connectivity", fail_connect)
    checks = run_preflight(
        scenario,
        target="http://127.0.0.1:8000/webhook",
        environment={"HOOKSTORM_SECRET": "private-secret"},
        connect=True,
    )

    assert _check(checks, "Network", "FAIL").message.startswith(
        "could not reach the endpoint socket: TimeoutError"
    )


def test_preflight_reports_invalid_scenario_and_continues_other_checks(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    checks = run_preflight(
        missing,
        target="http://127.0.0.1:8000/webhook",
        environment={},
    )

    assert _check(checks, "Scenario", "FAIL")
    assert _check(checks, "Target", "PASS")
    assert _check(checks, "Signing secret", "INFO")


def test_doctor_cli_does_not_print_target_tokens_or_secret(tmp_path, monkeypatch, capsys) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv(
        "HOOKSTORM_TARGET_URL",
        "https://staging.example.test/webhook?token=private-token",
    )
    monkeypatch.setenv("HOOKSTORM_SECRET", "private-secret")

    assert main(["doctor", str(scenario)]) == 0

    output = capsys.readouterr().out
    assert "Preflight: READY" in output
    assert "private-token" not in output
    assert "private-secret" not in output
    assert "full URL hidden" in output


def test_doctor_json_redacts_verification_credentials(tmp_path, monkeypatch, capsys) -> None:
    scenario = _verification_scenario_file(tmp_path)
    secret = "json-output-must-not-contain-this"
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", "https://staging.example.test/webhook")
    monkeypatch.setenv("HOOKSTORM_VERIFY_TOKEN", secret)

    assert main(["doctor", str(scenario), "--format", "json"]) == 0

    output = capsys.readouterr().out
    document = json.loads(output)
    auth_check = next(
        item for item in document["checks"] if item["label"] == "Verification auth"
    )
    assert auth_check["status"] == "PASS"
    assert "HOOKSTORM_VERIFY_TOKEN" in auth_check["message"]
    assert secret not in output


def test_doctor_cli_returns_one_for_missing_staging_settings(tmp_path, monkeypatch, capsys) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.delenv("HOOKSTORM_TARGET_URL", raising=False)
    monkeypatch.delenv("HOOKSTORM_SECRET", raising=False)

    assert main(["doctor", str(scenario)]) == 1
    output = capsys.readouterr().out
    assert "Preflight: NOT READY" in output
    assert "HOOKSTORM_TARGET_URL" in output
    assert "HOOKSTORM_SECRET" in output


def test_doctor_cli_rejects_out_of_range_timeout(tmp_path, capsys) -> None:
    scenario = _scenario_file(tmp_path)

    assert main(["doctor", str(scenario), "--timeout", "60"]) == 2
    assert "--timeout must be between 0.1 and 30 seconds" in capsys.readouterr().err


def test_verbose_connectivity_reports_ipv4_ipv6_and_partial_failures(monkeypatch) -> None:
    resolved = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", 8080)),
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:db8::10", 8080, 0, 0)),
    ]
    monkeypatch.setattr("hookstorm.doctor.socket.getaddrinfo", lambda *_args, **_kwargs: resolved)

    class FakeSocket:
        def __init__(self, family, _socktype, _proto):
            self.family = family

        def settimeout(self, _timeout):
            pass

        def connect(self, sockaddr):
            if self.family == socket.AF_INET6:
                raise OSError("network unreachable")

        def close(self):
            pass

    monkeypatch.setattr("hookstorm.doctor.socket.socket", FakeSocket)

    result = _check_connectivity_detailed(
        urlsplit("http://staging.example.test:8080/webhook"),
        timeout=1.0,
    )

    assert result.status == "WARN"
    assert result.message == "1/2 address(es) reachable; 1 address(es) failed"
    joined = "\n".join(result.details)
    assert "DNS IPv4: 192.0.2.10" in joined
    assert "DNS IPv6: 2001:db8::10" in joined
    assert "IPv4 192.0.2.10: TCP connected in" in joined
    assert "IPv6 2001:db8::10: connection failed" in joined
    assert "DNS lookup completed in" in joined


def test_verbose_https_reports_tls_and_certificate_metadata(monkeypatch) -> None:
    resolved = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.20", 443)),
    ]
    monkeypatch.setattr("hookstorm.doctor.socket.getaddrinfo", lambda *_args, **_kwargs: resolved)

    class FakeSocket:
        def __init__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _sockaddr):
            pass

        def close(self):
            pass

    class FakeTlsSocket:
        def version(self):
            return "TLSv1.3"

        def cipher(self):
            return ("TLS_AES_128_GCM_SHA256", "TLSv1.3", 128)

        def getpeercert(self):
            return {
                "subject": ((("commonName", "staging.example.test"),),),
                "issuer": ((("organizationName", "Example Test CA"),),),
                "notAfter": "Dec 01 00:00:00 2030 GMT",
            }

        def close(self):
            pass

    class FakeContext:
        def wrap_socket(self, _connection, *, server_hostname):
            assert server_hostname == "staging.example.test"
            return FakeTlsSocket()

    monkeypatch.setattr("hookstorm.doctor.socket.socket", FakeSocket)
    monkeypatch.setattr("hookstorm.doctor.ssl.create_default_context", FakeContext)

    result = _check_connectivity_detailed(
        urlsplit("https://staging.example.test/webhook"),
        timeout=1.0,
    )

    assert result.status == "PASS"
    assert result.message == "all 1 resolved address(es) reachable with verified TLS"
    joined = "\n".join(result.details)
    assert "TLSv1.3" in joined
    assert "cipher: TLS_AES_128_GCM_SHA256" in joined
    assert "Certificate subject: commonName=staging.example.test" in joined
    assert "Certificate issuer: organizationName=Example Test CA" in joined
    assert "Certificate expires: 2030-12-01T00:00:00+00:00" in joined


def test_verbose_dns_resolution_failure_is_a_failed_network_check(monkeypatch) -> None:
    def fail_dns(*_args, **_kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr("hookstorm.doctor.socket.getaddrinfo", fail_dns)

    result = _check_connectivity_detailed(
        urlsplit("https://staging.example.test/webhook"),
        timeout=1.0,
    )

    assert result.status == "FAIL"
    assert "DNS lookup failed" in result.message
    assert "DNS lookup time:" in result.details[0]


def test_doctor_verbose_implies_connect_and_prints_details(
    tmp_path, monkeypatch, capsys
) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", "https://staging.example.test/webhook")
    monkeypatch.setenv("HOOKSTORM_SECRET", "private-secret")
    monkeypatch.setattr(
        "hookstorm.doctor._check_connectivity_detailed",
        lambda *_args: ConnectivityResult(
            "PASS",
            "all 1 resolved address(es) reachable with verified TLS",
            ("DNS lookup completed in 0.50 ms (1 unique address(es))", "  TLS: TLSv1.3"),
        ),
    )

    assert main(["doctor", str(scenario), "--verbose"]) == 0

    output = capsys.readouterr().out
    assert "Preflight: READY" in output
    assert "DNS lookup completed in 0.50 ms" in output
    assert "TLS: TLSv1.3" in output
    assert "private-secret" not in output


def test_doctor_json_output_is_versioned_and_secret_safe(
    tmp_path, monkeypatch, capsys
) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv(
        "HOOKSTORM_TARGET_URL",
        "https://staging.example.test/webhook?token=private-token",
    )
    monkeypatch.setenv("HOOKSTORM_SECRET", "private-secret")

    assert main(["doctor", str(scenario), "--format", "json"]) == 0

    output = capsys.readouterr().out
    document = json.loads(output)
    assert set(document) == {
        "schema_version",
        "tool",
        "tool_version",
        "command",
        "scenario",
        "status",
        "ready",
        "exit_code",
        "network",
        "summary",
        "checks",
    }
    assert document["schema_version"] == 1
    assert document["tool"] == "HookStorm"
    assert document["command"] == "doctor"
    assert document["scenario"] == str(scenario)
    assert document["status"] == "READY"
    assert document["ready"] is True
    assert document["exit_code"] == 0
    assert document["network"] == {
        "mode": "none",
        "requested": False,
        "performed": False,
    }
    assert document["summary"] == {
        "total_checks": len(document["checks"]),
        "passed": 4,
        "failed": 0,
        "warnings": 1,
        "info": 1,
    }
    auth_check = next(
        item for item in document["checks"] if item["label"] == "Verification auth"
    )
    assert auth_check["status"] == "PASS"
    assert "not required" in auth_check["message"]
    assert all(
        set(check) == {"status", "label", "message", "details"}
        and isinstance(check["details"], list)
        for check in document["checks"]
    )
    assert "private-token" not in output
    assert "private-secret" not in output


def test_doctor_json_output_preserves_failure_exit_code(
    tmp_path, monkeypatch, capsys
) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.delenv("HOOKSTORM_TARGET_URL", raising=False)
    monkeypatch.delenv("HOOKSTORM_SECRET", raising=False)

    assert main(["doctor", str(scenario), "--format", "json"]) == 1

    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "NOT_READY"
    assert document["ready"] is False
    assert document["exit_code"] == 1
    assert document["summary"]["failed"] == 2
    failed_labels = {
        check["label"] for check in document["checks"] if check["status"] == "FAIL"
    }
    assert failed_labels == {"Target", "Signing secret"}


def test_doctor_json_verbose_includes_network_details(
    tmp_path, monkeypatch, capsys
) -> None:
    scenario = _scenario_file(tmp_path)
    monkeypatch.setenv("HOOKSTORM_TARGET_URL", "https://staging.example.test/webhook")
    monkeypatch.setenv("HOOKSTORM_SECRET", "private-secret")
    monkeypatch.setattr(
        "hookstorm.doctor._check_connectivity_detailed",
        lambda *_args: ConnectivityResult(
            "PASS",
            "all 1 resolved address(es) reachable with verified TLS",
            (
                "DNS IPv4: 192.0.2.25",
                "IPv4 192.0.2.25: TCP connected in 1.00 ms",
                "TLS: TLSv1.3; cipher: TLS_AES_128_GCM_SHA256",
                "Certificate expires: 2030-12-01T00:00:00+00:00",
            ),
        ),
    )

    assert main(["doctor", str(scenario), "--verbose", "--format", "json"]) == 0

    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["network"] == {
        "mode": "verbose",
        "requested": True,
        "performed": True,
    }
    network = next(check for check in document["checks"] if check["label"] == "Network")
    assert network["status"] == "PASS"
    assert network["details"][0] == "DNS IPv4: 192.0.2.25"
    assert "TLSv1.3" in network["details"][2]
    assert "private-secret" not in output
    assert "Preflight:" not in output
