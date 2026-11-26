from __future__ import annotations

import xml.etree.ElementTree as ET

from hookstorm.report import render_html, render_json, render_junit, write_report


def test_report_serialization_escapes_html() -> None:
    result = {
        "scenario": "<script>alert(1)</script>",
        "target": "http://127.0.0.1/hook",
        "version": "0.1.0",
        "duration_ms": 1.5,
        "summary": {"success": True, "passed": 1, "total_deliveries": 1, "attempts": 1},
        "deliveries": [{
            "event_type": "<img>",
            "event_id": "evt_1<script>alert(2)</script>",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 1.0, "error": None}],
        }],
    }
    html = render_html(result)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert '"scenario": "<script>alert(1)</script>"' in render_json(result)



def test_report_contains_interactive_searchable_timeline() -> None:
    result = {
        "scenario": "provider delivery",
        "target": "http://127.0.0.1:8000/webhook",
        "version": "0.1.0",
        "started_at": "2026-11-26T12:00:00+00:00",
        "duration_ms": 42.5,
        "summary": {
            "success": True,
            "passed": 1,
            "total_deliveries": 1,
            "attempts": 2,
        },
        "deliveries": [{
            "event_type": "invoice.paid",
            "event_id": "evt_123",
            "duplicate_index": 0,
            "signature_provider": "stripe",
            "passed": True,
            "attempts": [
                {
                    "attempt": 1,
                    "elapsed_ms": 0.5,
                    "status_code": 503,
                    "latency_ms": 10.0,
                    "error": None,
                    "passed": False,
                },
                {
                    "attempt": 2,
                    "elapsed_ms": 20.5,
                    "status_code": 200,
                    "latency_ms": 22.0,
                    "error": None,
                    "passed": True,
                },
            ],
        }],
    }
    html_report = render_html(result)
    assert 'id="status-filter"' in html_report
    assert 'id="event-search"' in html_report
    assert 'id="expand-all"' in html_report
    assert "data-state='passed'" in html_report
    assert "data-search='invoice.paid evt_123 stripe" in html_report
    assert "Attempt timeline for evt_123" in html_report
    assert "+20 ms" in html_report
    assert "Stripe" not in html_report  # Provider key stays normalized and lower-case.
    assert "addEventListener" in html_report


def test_report_escapes_event_data_in_attributes_and_attempt_notes() -> None:
    result = {
        "scenario": "safe",
        "target": "http://127.0.0.1/hook",
        "version": "0.1.0",
        "duration_ms": 1.0,
        "summary": {"success": False, "passed": 0, "total_deliveries": 1, "attempts": 1},
        "deliveries": [{
            "event_type": "invoice.paid",
            "event_id": '" onmouseover="alert(1)',
            "duplicate_index": 0,
            "passed": False,
            "attempts": [{
                "status_code": 500,
                "latency_ms": 1.0,
                "elapsed_ms": 0.0,
                "error": "<script>alert(1)</script>",
            }],
        }],
    }
    html_report = render_html(result)
    assert 'data-search="" onmouseover=' not in html_report
    assert "<script>alert(1)</script>" not in html_report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_report



def test_junit_report_contains_one_testcase_per_delivery_and_counts_failures() -> None:
    result = {
        "scenario": "junit smoke",
        "version": "0.1.0",
        "run_id": "run_safe_123",
        "summary": {"success": False},
        "deliveries": [
            {
                "event_type": "invoice.paid",
                "event_id": "evt_pass",
                "duplicate_index": 0,
                "passed": True,
                "attempts": [{"status_code": 200, "latency_ms": 15.0, "error": None}],
            },
            {
                "event_type": "orders.created",
                "event_id": "evt_failure",
                "duplicate_index": 0,
                "passed": False,
                "attempts": [
                    {"status_code": 503, "latency_ms": 10.0, "error": None},
                    {"status_code": 401, "latency_ms": 20.0, "error": "signature mismatch"},
                ],
            },
            {
                "event_type": "report.ready",
                "event_id": "evt_error",
                "duplicate_index": 1,
                "passed": False,
                "attempts": [{"status_code": None, "latency_ms": 100.0, "error": "request timed out"}],
            },
        ],
    }
    xml_report = render_junit(result)
    root = ET.fromstring(xml_report)
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"
    assert root.attrib["skipped"] == "0"
    cases = root.findall("testcase")
    assert len(cases) == 3
    assert cases[0].find("failure") is None
    assert cases[1].find("failure").attrib["type"] == "AssertionFailure"
    assert cases[2].find("error").attrib["type"] == "DeliveryError"
    assert "signature mismatch" in xml_report
    assert "request timed out" in xml_report


def test_junit_report_does_not_include_payloads_secrets_or_url_query() -> None:
    result = {
        "scenario": "private data check",
        "version": "0.1.0",
        "run_id": "run_123",
        "target": "http://127.0.0.1/hook?token=do-not-export",
        "secret": "super-secret",
        "payload": {"card": "4111111111111111"},
        "summary": {"success": True},
        "deliveries": [{
            "event_type": "payment.created",
            "event_id": "evt_public_id",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 3.0, "error": None}],
        }],
    }
    xml_report = render_junit(result)
    assert "do-not-export" not in xml_report
    assert "super-secret" not in xml_report
    assert "4111111111111111" not in xml_report
    assert "evt_public_id" in xml_report


def test_write_report_supports_junit_xml(tmp_path) -> None:
    result = {
        "scenario": "write JUnit",
        "version": "0.1.0",
        "run_id": "run_1",
        "summary": {"success": True},
        "deliveries": [{
            "event_type": "ping",
            "event_id": "evt_1",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 2.0, "error": None}],
        }],
    }
    output = write_report(result, tmp_path / "reports" / "hookstorm.xml", "junit")
    assert output.is_file()
    assert ET.parse(output).getroot().attrib["tests"] == "1"
