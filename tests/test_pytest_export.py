from __future__ import annotations

from hookstorm.config import validate_scenario
from hookstorm.pytest_export import render_pytest, write_pytest


def example_scenario() -> dict:
    return validate_scenario({
        "version": 1,
        "name": "integration export",
        "events": [{
            "id": "invoice.paid",
            "event_id": "evt_export_1",
            "payload": {"invoice_id": "inv_1"},
            "expect_json": {"result": {"accepted": True}},
        }],
    })


def test_rendered_pytest_module_is_valid_python() -> None:
    source = render_pytest(example_scenario())
    compile(source, "generated_hookstorm_test.py", "exec")
    assert "HOOKSTORM_TARGET" in source
    assert "HOOKSTORM_SECRET" in source
    assert "HOOKSTORM_ALLOW_REMOTE" in source
    assert "evt_export_1" in source
    assert "pytest.mark.integration" in source


def test_write_pytest_creates_parent_directories(tmp_path) -> None:
    output = tmp_path / "generated" / "tests" / "test_webhook.py"
    written = write_pytest(example_scenario(), output)
    assert written == output
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == render_pytest(example_scenario())
