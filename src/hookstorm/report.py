"""JSON and self-contained interactive HTML report rendering."""

from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def render_json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"


def _render_attempt(attempt: dict[str, Any], index: int, *, final: bool, passed: bool) -> str:
    """Build one escaped attempt marker for the delivery track."""
    status_code = attempt.get("status_code")
    status_label = str(status_code) if status_code is not None else "ERR"
    latency = float(attempt.get("latency_ms", 0.0))
    elapsed = float(attempt.get("elapsed_ms", 0.0))
    error = html.escape(str(attempt.get("error") or ""))
    state = "pass" if final and passed else "fail" if final else "retry"
    title = html.escape(
        f"Attempt {index}: HTTP {status_label}; {latency:.2f} ms; +{elapsed:.2f} ms",
        quote=True,
    )
    error_markup = f"<em>{error}</em>" if error else ""
    return (
        f"<li class='attempt {state}' title='{title}'>"
        f"<span class='attempt-index'>#{index}</span>"
        f"<strong>{html.escape(status_label)}</strong>"
        f"<span>{latency:.2f} ms</span>"
        f"<small>+{elapsed:.0f} ms</small>"
        f"{error_markup}"
        "</li>"
    )


def render_html(result: dict[str, Any]) -> str:
    """Render a self-contained report with searchable, filterable delivery timelines."""
    summary = result["summary"]
    status = "PASSED" if summary["success"] else "FAILED"
    deliveries = list(result["deliveries"])
    deliveries.sort(
        key=lambda item: (
            float(item.get("attempts", [{}])[0].get("elapsed_ms", 0.0))
            if item.get("attempts")
            else 0.0
        )
    )

    cards: list[str] = []
    for delivery in deliveries:
        attempts = delivery.get("attempts", [])
        if not attempts:
            continue
        passed = bool(delivery["passed"])
        state = "passed" if passed else "failed"
        badge = "PASS" if passed else "FAIL"
        event_type = html.escape(str(delivery["event_type"]))
        event_id = html.escape(str(delivery["event_id"]))
        signature_provider = html.escape(str(delivery.get("signature_provider", "generic")))
        duplicate_index = int(delivery.get("duplicate_index", 0))
        error_text = " ".join(str(item.get("error") or "") for item in attempts)
        searchable = html.escape(
            f"{delivery['event_type']} {delivery['event_id']} {signature_provider} {error_text}".lower(),
            quote=True,
        )
        rail = "".join(
            _render_attempt(
                attempt,
                index,
                final=index == len(attempts),
                passed=passed,
            )
            for index, attempt in enumerate(attempts, start=1)
        )
        final_attempt = attempts[-1]
        final_status = (
            str(final_attempt["status_code"])
            if final_attempt.get("status_code") is not None
            else "ERR"
        )
        note = html.escape(str(final_attempt.get("error") or ""))
        note_markup = f"<p>{note}</p>" if note else "<p>No error details recorded.</p>"
        cards.append(
            f"<article class='delivery-card {state}' data-state='{state}' data-search='{searchable}'>"
            "<div class='delivery-top'>"
            "<div class='delivery-identity'>"
            f"<span class='event-type'>{event_type}</span>"
            f"<code>{event_id}</code>"
            "</div>"
            "<div class='delivery-meta'>"
            f"<span class='badge {'good' if passed else 'bad'}'>{badge}</span>"
            f"<span>HTTP {html.escape(final_status)}</span>"
            f"<span>duplicate #{duplicate_index}</span>"
            f"<span class='provider'>{signature_provider}</span>"
            "</div></div>"
            f"<ol class='attempt-rail' aria-label='Attempt timeline for {event_id}'>{rail}</ol>"
            "<details class='attempt-details'>"
            f"<summary>Inspect {len(attempts)} attempt(s)</summary>"
            f"<div class='attempt-notes'>{note_markup}</div>"
            "</details></article>"
        )

    started_at = html.escape(str(result.get("started_at", "unknown")))
    scenario = html.escape(str(result["scenario"]))
    target = html.escape(str(result["target"]))
    version = html.escape(str(result["version"]))
    timeline_content = "".join(cards) if cards else '<p class="empty">No delivery attempts were recorded.</p>'
    verification = result.get("verification")
    verification_markup = ""
    if verification is not None:
        verify_state = "PASS" if verification.get("passed") else "FAIL"
        verify_status = (
            str(verification["status_code"])
            if verification.get("status_code") is not None
            else "ERR"
        )
        verify_error = html.escape(str(verification.get("error") or ""))
        verify_target = html.escape(str(verification.get("target") or ""))
        verification_markup = (
            "<section class='verification'>"
            "<h2>Business-state verification</h2>"
            f"<p><strong>{verify_state}</strong> · GET {verify_target} · "
            f"HTTP {verify_status} · {float(verification.get('latency_ms', 0.0)):.2f} ms</p>"
            f"<p class='muted'>{verify_error}</p>"
            "</section>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>HookStorm report — {scenario}</title>
<style>
:root{{font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color-scheme:light dark}}
*{{box-sizing:border-box}}body{{margin:0;background:Canvas;color:CanvasText}}
main{{max-width:1180px;margin:40px auto;padding:0 24px 48px}}
h1{{font-size:clamp(1.7rem,4vw,2.25rem);margin:5px 0 8px;letter-spacing:-.03em}}
h2{{font-size:1.15rem;margin:32px 0 14px}}.muted{{color:GrayText}}.eyebrow{{font-size:.76rem;letter-spacing:.14em;font-weight:750}}
.target{{overflow-wrap:anywhere;font-size:.9rem}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:26px 0}}
.card{{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:16px 18px;min-width:0}}
.card strong{{display:block;font-size:1.45rem;margin-bottom:3px}}.card span{{font-size:.82rem;color:GrayText}}
.toolbar{{display:flex;flex-wrap:wrap;align-items:end;gap:12px;padding:14px;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;margin-bottom:12px}}
.field{{display:grid;gap:5px;font-size:.8rem;color:GrayText}}input,select,button{{font:inherit;color:CanvasText;background:Canvas;border:1px solid color-mix(in srgb,CanvasText 30%,transparent);border-radius:8px;padding:8px 10px}}
input{{min-width:min(280px,70vw)}}button{{cursor:pointer;font-weight:650}}button:hover,summary:hover{{border-color:Highlight}}
#result-count{{margin-left:auto;color:GrayText;font-size:.82rem;padding:8px 0}}
.verification{{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:16px 18px;margin:18px 0}}.verification h2{{margin:0 0 8px}}.verification p{{margin:4px 0;font-size:.88rem;overflow-wrap:anywhere}}.timeline{{display:grid;gap:10px}}.delivery-card{{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:16px 18px;min-width:0}}
.delivery-card[hidden]{{display:none}}.delivery-top{{display:flex;gap:16px;justify-content:space-between;align-items:start;flex-wrap:wrap}}
.delivery-identity{{display:grid;gap:4px;min-width:0}}.event-type{{font-weight:750}}.delivery-identity code{{font-size:.8rem;color:GrayText;overflow-wrap:anywhere}}
.delivery-meta{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:.78rem;color:GrayText}}
.badge{{font-size:.72rem;font-weight:800;padding:4px 9px;border-radius:999px;letter-spacing:.03em}}
.good{{background:#14532d;color:#dcfce7}}.bad{{background:#7f1d1d;color:#fee2e2}}
.provider{{border:1px solid color-mix(in srgb,CanvasText 20%,transparent);padding:3px 7px;border-radius:999px}}
.attempt-rail{{list-style:none;margin:15px 0 10px;padding:0;display:flex;gap:8px;overflow-x:auto}}
.attempt{{position:relative;display:grid;gap:3px;flex:1 0 130px;max-width:220px;min-width:125px;padding:10px 11px;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:10px;background:color-mix(in srgb,CanvasText 4%,Canvas);font-size:.78rem}}
.attempt::before{{content:"";position:absolute;left:0;right:0;top:0;height:3px;border-radius:10px 10px 0 0;background:GrayText}}
.attempt.pass::before{{background:#16a34a}}.attempt.fail::before{{background:#dc2626}}.attempt.retry::before{{background:#d97706}}
.attempt-index,.attempt small{{color:GrayText;font-size:.7rem}}.attempt strong{{font-size:1.03rem}}.attempt em{{font-size:.72rem;font-style:normal;color:#dc6b6b;overflow-wrap:anywhere}}
.attempt-details summary{{cursor:pointer;display:inline-block;color:GrayText;font-size:.8rem;padding:4px 0}}
.attempt-notes{{font-size:.82rem;color:GrayText;overflow-wrap:anywhere}}.empty{{padding:26px;text-align:center;color:GrayText;border:1px dashed GrayText;border-radius:12px}}
footer{{margin-top:32px;color:GrayText;font-size:.8rem}}
@media(max-width:700px){{main{{margin:26px auto;padding:0 14px 36px}}.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.delivery-card{{padding:13px}}.delivery-top{{display:grid}}#result-count{{width:100%;margin:0}}}}
@media(prefers-reduced-motion:no-preference){{.delivery-card{{transition:opacity .15s ease}}}}
</style>
</head>
<body><main>
<p class="muted eyebrow">HOOKSTORM / RUN REPORT</p>
<h1>{scenario}</h1>
<p class="muted target">{target}</p>
<p class="muted" style="font-size:.78rem">Started: {started_at}</p>
<div class="grid">
<div class="card"><strong>{status}</strong><span>Overall result</span></div>
<div class="card"><strong>{summary['passed']}/{summary['total_deliveries']}</strong><span>Deliveries passed</span></div>
<div class="card"><strong>{summary['attempts']}</strong><span>HTTP attempts</span></div>
<div class="card"><strong>{result['duration_ms']:.0f} ms</strong><span>Total duration</span></div>
</div>
{verification_markup}
<h2>Delivery timeline</h2>
<div class="toolbar" role="region" aria-label="Timeline filters">
<label class="field">Result
<select id="status-filter">
<option value="all">All deliveries</option>
<option value="passed">Passed</option>
<option value="failed">Failed</option>
</select></label>
<label class="field">Find event or ID
<input id="event-search" type="search" placeholder="Type to filter events" autocomplete="off">
</label>
<button id="expand-all" type="button">Expand all attempts</button>
<span id="result-count" aria-live="polite"></span>
</div>
<section class="timeline" id="timeline" aria-label="Webhook event delivery attempts">
{timeline_content}
</section>
<footer>Generated by HookStorm {version} · Attempts are ordered by elapsed run time. Payloads, request headers and secrets are not included.</footer>
</main>
<script>
(() => {{
  const cards = Array.from(document.querySelectorAll(".delivery-card"));
  const statusFilter = document.getElementById("status-filter");
  const searchInput = document.getElementById("event-search");
  const count = document.getElementById("result-count");
  const expandButton = document.getElementById("expand-all");
  const applyFilters = () => {{
    const state = statusFilter.value;
    const query = searchInput.value.trim().toLowerCase();
    let visible = 0;
    for (const card of cards) {{
      const stateMatches = state === "all" || card.dataset.state === state;
      const queryMatches = !query || card.dataset.search.includes(query);
      card.hidden = !(stateMatches && queryMatches);
      if (!card.hidden) visible += 1;
    }}
    count.textContent = visible + " of " + cards.length + " deliveries";
  }};
  statusFilter.addEventListener("change", applyFilters);
  searchInput.addEventListener("input", applyFilters);
  expandButton.addEventListener("click", () => {{
    const details = cards.filter(card => !card.hidden).map(card => card.querySelector("details"));
    const expand = details.some(detail => !detail.open);
    details.forEach(detail => {{ detail.open = expand; }});
    expandButton.textContent = expand ? "Collapse all attempts" : "Expand all attempts";
  }});
  applyFilters();
}})();
</script>
</body>
</html>"""





def render_junit(result: dict[str, Any]) -> str:
    """Render delivery results as JUnit XML without embedding request data or secrets."""
    deliveries = result["deliveries"]
    failures = 0
    errors = 0
    duration_seconds = 0.0

    for delivery in deliveries:
        attempts = delivery.get("attempts", [])
        duration_seconds += sum(float(item.get("latency_ms", 0.0)) for item in attempts) / 1000
        if not delivery.get("passed"):
            final = attempts[-1] if attempts else {}
            if final.get("status_code") is None:
                errors += 1
            else:
                failures += 1

    verification = result.get("verification")
    if verification is not None:
        duration_seconds += float(verification.get("latency_ms", 0.0)) / 1000
        if not verification.get("passed"):
            if verification.get("status_code") is None:
                errors += 1
            else:
                failures += 1

    suite = ET.Element(
        "testsuite",
        {
            "name": str(result["scenario"]),
            "tests": str(len(deliveries) + (1 if verification is not None else 0)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": "0",
            "time": f"{duration_seconds:.6f}",
        },
    )
    if result.get("started_at"):
        suite.set("timestamp", str(result["started_at"]))

    properties = ET.SubElement(suite, "properties")
    ET.SubElement(
        properties,
        "property",
        {"name": "hookstorm.version", "value": str(result.get("version", ""))},
    )
    ET.SubElement(
        properties,
        "property",
        {"name": "hookstorm.run_id", "value": str(result.get("run_id", ""))},
    )

    for delivery in deliveries:
        attempts = delivery.get("attempts", [])
        final = attempts[-1] if attempts else {}
        case_time = sum(float(item.get("latency_ms", 0.0)) for item in attempts) / 1000
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": str(delivery.get("event_type", "webhook")),
                "name": (
                    f"{delivery.get('event_id', 'unknown')}"
                    f"[duplicate={delivery.get('duplicate_index', 0)}]"
                ),
                "time": f"{case_time:.6f}",
            },
        )
        if delivery.get("passed"):
            continue

        status_code = final.get("status_code")
        detail = str(final.get("error") or "")
        attempts_text = f" after {len(attempts)} attempt(s)" if attempts else ""
        if status_code is None:
            message = detail or "No HTTP response was received"
            ET.SubElement(
                testcase,
                "error",
                {
                    "message": f"Webhook delivery error{attempts_text}: {message}",
                    "type": "DeliveryError",
                },
            )
        else:
            message = f"Final HTTP status: {status_code}"
            if detail:
                message += f"; {detail}"
            failure = ET.SubElement(
                testcase,
                "failure",
                {
                    "message": f"Webhook assertion failed{attempts_text}: {message}",
                    "type": "AssertionFailure",
                },
            )
            failure.text = message

    if verification is not None:
        verify_time = float(verification.get("latency_ms", 0.0)) / 1000
        verify_case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "hookstorm.verification",
                "name": "business-state probe",
                "time": f"{verify_time:.6f}",
            },
        )
        if not verification.get("passed"):
            message = str(verification.get("error") or "Business-state verification failed")
            status_code = verification.get("status_code")
            if status_code is None:
                ET.SubElement(
                    verify_case,
                    "error",
                    {"message": message, "type": "VerificationError"},
                )
            else:
                failure = ET.SubElement(
                    verify_case,
                    "failure",
                    {"message": message, "type": "VerificationFailure"},
                )
                failure.text = message

    ET.indent(suite, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        suite, encoding="unicode"
    ) + "\n"


def write_report(result: dict[str, Any], path: str | Path, format_name: str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "html":
        content = render_html(result)
    elif format_name == "junit":
        content = render_junit(result)
    else:
        content = render_json(result)
    output.write_text(content, encoding="utf-8")
    return output
