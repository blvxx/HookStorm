from __future__ import annotations

from hookstorm.report import render_html, render_json


def test_report_serialization_escapes_html() -> None:
    result = {
        "scenario": "<script>alert(1)</script>",
        "target": "http://127.0.0.1/hook",
        "version": "0.1.0",
        "duration_ms": 1.5,
        "summary": {"success": True, "passed": 1, "total_deliveries": 1, "attempts": 1},
        "deliveries": [{
            "event_type": "<img>",
            "event_id": "evt_1",
            "duplicate_index": 0,
            "passed": True,
            "attempts": [{"status_code": 200, "latency_ms": 1.0, "error": None}],
        }],
    }
    html = render_html(result)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert '"scenario": "<script>alert(1)</script>"' in render_json(result)
