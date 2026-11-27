"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from hookstorm import __version__
from hookstorm.config import load_scenario
from hookstorm.doctor import build_preflight_document, run_preflight
from hookstorm.history import (
    HistoryError,
    compare_history_file,
    compare_history_runs,
    export_history_run,
    get_run,
    list_runs,
    result_metrics,
    save_run,
)
from hookstorm.pytest_export import write_pytest
from hookstorm.report import render_json, write_report
from hookstorm.runner import run_scenario
from hookstorm.scaffold import initialize_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hookstorm",
        description="Test webhook delivery behavior with repeatable failure scenarios.",
        epilog=(
            "Loopback targets are allowed by default. Remote HTTPS requires --allow-remote; "
            "remote plaintext HTTP additionally requires --allow-insecure-http. "
            "Use these flags only for systems you are authorized to test."
        ),
    )
    parser.add_argument("--version", action="version", version=f"HookStorm {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", help="create a starter webhook scenario and GitHub Actions workflow"
    )
    init.add_argument("--scenario-path", default="scenarios/webhook.yaml")
    init.add_argument("--workflow-path", default=".github/workflows/webhook-regression.yml")
    init.add_argument(
        "--signature-provider",
        choices=("generic", "stripe", "github", "shopify", "standard_webhooks"),
        help="webhook signature format (prompted in an interactive terminal; defaults to generic)",
    )
    init.add_argument(
        "--event-type",
        help="webhook event type (prompted in an interactive terminal; defaults to test.ping)",
    )
    init.add_argument(
        "--event-id",
        help="stable event ID (prompted in an interactive terminal; defaults to evt_hookstorm_init_001)",
    )
    init.add_argument(
        "--max-attempts",
        type=int,
        help="total delivery attempts including the first request (1–10; interactive default: 1)",
    )
    init.add_argument(
        "--duplicate-count",
        type=int,
        help="additional deliveries of the same event (0–100; interactive default: 0)",
    )
    init.add_argument("--no-workflow", action="store_true", help="create only the YAML scenario")
    init.add_argument("--force", action="store_true", help="overwrite generated files if they exist")

    run = subparsers.add_parser("run", help="execute a YAML scenario against a webhook endpoint")
    run.add_argument("scenario", help="path to a scenario YAML file")
    run.add_argument("--target", required=True, help="webhook URL; localhost only unless opted in")
    run.add_argument("--allow-remote", action="store_true", help="allow a remote HTTPS target")
    run.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="allow unencrypted remote HTTP; requires --allow-remote",
    )
    run.add_argument("--secret-env", default="HOOKSTORM_SECRET", help="environment variable for HMAC secret")
    run.add_argument("--format", choices=("html", "json", "junit"), default="html", help="report format")
    run.add_argument("--output", help="write a report to this path")
    run.add_argument("--history-db", help="history database path")
    run.add_argument("--no-history", action="store_true", help="do not persist this run")

    history = subparsers.add_parser("history", help="inspect and compare local run history")
    history_sub = history.add_subparsers(dest="history_command", required=True)
    history_list = history_sub.add_parser("list", help="list recent runs")
    history_list.add_argument("--limit", type=int, default=20)
    history_list.add_argument("--scenario", help="filter by exact scenario name")
    history_list.add_argument("--db", help="history database path")
    history_show = history_sub.add_parser("show", help="show one stored run")
    history_show.add_argument("run_id")
    history_show.add_argument("--db", help="history database path")
    history_compare = history_sub.add_parser("compare", help="compare a run against a baseline")
    history_compare.add_argument("baseline_run_id")
    history_compare.add_argument("--current", default="latest")
    history_compare.add_argument("--latency-threshold-pct", type=float, default=20.0)
    history_compare.add_argument(
        "--allow-config-change",
        action="store_true",
        help="accept a changed scenario configuration when intentionally refreshing a baseline",
    )
    history_compare.add_argument("--db", help="history database path")
    history_compare_file = history_sub.add_parser(
        "compare-file", help="compare the latest run against a portable JSON baseline"
    )
    history_compare_file.add_argument("baseline_file", help="exported HookStorm baseline JSON")
    history_compare_file.add_argument("--current", default="latest")
    history_compare_file.add_argument("--latency-threshold-pct", type=float, default=20.0)
    history_compare_file.add_argument(
        "--allow-config-change",
        action="store_true",
        help="accept a changed scenario configuration when intentionally refreshing a baseline",
    )
    history_compare_file.add_argument("--db", help="history database path")
    history_export = history_sub.add_parser(
        "export", help="export one sanitized run as a portable JSON baseline"
    )
    history_export.add_argument("run_id", help="run ID or 'latest'")
    history_export.add_argument("--output", required=True, help="output JSON path")
    history_export.add_argument("--db", help="history database path")

    doctor = subparsers.add_parser(
        "doctor", help="check staging integration configuration without sending webhook events"
    )
    doctor.add_argument(
        "scenario", nargs="?", default="scenarios/webhook.yaml", help="scenario YAML (default: scenarios/webhook.yaml)"
    )
    doctor.add_argument("--target", help="endpoint URL; defaults to HOOKSTORM_TARGET_URL")
    doctor.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="allow preflighting a remote plaintext HTTP endpoint",
    )
    doctor.add_argument("--secret-env", default="HOOKSTORM_SECRET", help="environment variable for HMAC secret")
    doctor.add_argument(
        "--connect",
        action="store_true",
        help="check DNS/TCP and HTTPS TLS connectivity without sending an HTTP request",
    )
    doctor.add_argument(
        "--verbose",
        action="store_true",
        help="show DNS addresses, per-address timing, and TLS certificate details (implies --connect)",
    )
    doctor.add_argument("--timeout", type=float, default=5.0, help="socket timeout in seconds (0.1–30; default: 5)")
    doctor.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )

    validate = subparsers.add_parser("validate", help="validate a scenario without sending requests")
    validate.add_argument("scenario", help="path to a scenario YAML file")

    export = subparsers.add_parser(
        "export-pytest", help="export a scenario as a pytest integration test"
    )
    export.add_argument("scenario", help="path to a scenario YAML file")
    export.add_argument(
        "--output", default="test_hookstorm_scenario.py", help="generated pytest module path"
    )
    return parser


def _print_summary(result: dict) -> None:
    summary = result["summary"]
    state = "PASS" if summary["success"] else "FAIL"
    print(f"\nHookStorm · {state} · {result['scenario']}")
    print(f"Target: {result['target']}")
    print(
        f"Deliveries: {summary['passed']}/{summary['total_deliveries']} passed  |  "
        f"Attempts: {summary['attempts']}  |  Duration: {result['duration_ms']:.0f} ms"
    )
    for delivery in result["deliveries"]:
        latest = delivery["attempts"][-1]
        mark = "✓" if delivery["passed"] else "✗"
        status = latest["status_code"] if latest["status_code"] is not None else "ERR"
        print(
            f" {mark} {delivery['event_type']} [{delivery['event_id']}] "
            f"duplicate={delivery['duplicate_index']} attempts={len(delivery['attempts'])} "
            f"status={status}"
        )
        if latest["error"]:
            print(f"   {latest['error']}")
    verification = result.get("verification")
    if verification is not None:
        verify_mark = "✓" if verification["passed"] else "✗"
        verify_status = (
            verification["status_code"]
            if verification["status_code"] is not None
            else "ERR"
        )
        print(
            f" {verify_mark} Business-state verification "
            f"GET {verification['target']} status={verify_status}"
        )
        if verification["error"]:
            print(f"   {verification['error']}")


def _history_list_command(args: argparse.Namespace) -> int:
    runs = list_runs(args.db, limit=args.limit, scenario=args.scenario)
    if not runs:
        print("No runs found in local history.")
        return 0
    print("RUN ID         RESULT  PASS RATE  DELIVERIES  P95 LATENCY  SCENARIO")
    for item in runs:
        state = "PASS" if item["success"] else "FAIL"
        delivery_count = f"{item['passed']}/{item['total_deliveries']}"
        print(
            f"{item['run_id']:<14} {state:<7} "
            f"{item['pass_rate_pct']:>7.2f}%  {delivery_count:<10} "
            f"{item['p95_latency_ms']:>8.2f} ms  {item['scenario']}"
        )
        print(f"  {item['started_at']} · {item['duration_ms']:.0f} ms · {item['target']}")
    return 0


def _history_show_command(args: argparse.Namespace) -> int:
    result = get_run(args.run_id, args.db)
    metrics = result_metrics(result)
    state = "PASS" if result["summary"]["success"] else "FAIL"
    print(f"Run {result['run_id']} · {state} · {result['scenario']}")
    print(f"Started: {result['started_at']}")
    print(f"Target:  {result['target']}")
    print(
        f"Deliveries: {metrics['passed']}/{metrics['total_deliveries']} passed · "
        f"Attempts: {metrics['attempts']} · Duration: {metrics['duration_ms']:.0f} ms"
    )
    print(
        f"Pass rate: {metrics['pass_rate_pct']:.2f}% · "
        f"p50: {metrics['p50_latency_ms']:.2f} ms · "
        f"p95: {metrics['p95_latency_ms']:.2f} ms"
    )
    for delivery in result.get("deliveries", []):
        attempts = delivery.get("attempts", [])
        latest = attempts[-1] if attempts else {}
        state = "PASS" if delivery.get("passed") else "FAIL"
        status = latest.get("status_code")
        print(
            f"  {state:<4} {delivery.get('event_type')} "
            f"[{delivery.get('event_id')}] duplicate={delivery.get('duplicate_index', 0)} "
            f"attempts={len(attempts)} HTTP={status if status is not None else 'ERR'}"
        )
    return 0


def _print_history_comparison(comparison: dict) -> int:
    print(
        f"Comparing {comparison['current_run_id']} against baseline "
        f"{comparison['baseline_run_id']}"
    )
    print(
        f"Scenario: {comparison['current_scenario']} "
        f"(baseline: {comparison['baseline_scenario']})"
    )
    if not comparison["same_scenario"]:
        print("WARNING: scenario names differ; interpret coverage and metric changes carefully.")
    if comparison["same_configuration"] is False:
        if comparison["allow_config_change"]:
            print("WARNING: scenario configuration changed; explicit baseline override is active.")
        else:
            print(
                "ERROR: scenario configuration differs from the baseline. "
                "Use --allow-config-change only when intentionally accepting this change."
            )
    elif comparison["same_configuration"] is None:
        print(
            "WARNING: scenario configuration could not be verified because this baseline "
            "predates configuration fingerprints."
        )
    baseline = comparison["baseline"]
    current = comparison["current"]
    delta = comparison["delta"]
    print(
        f"Pass rate: {baseline['pass_rate_pct']:.2f}% -> "
        f"{current['pass_rate_pct']:.2f}% ({delta['pass_rate_pct']:+.2f} pp)"
    )
    print(
        f"Failed deliveries: {baseline['failed']} -> {current['failed']} "
        f"({delta['failed_deliveries']:+d})"
    )
    print(
        f"p95 delivery latency: {baseline['p95_latency_ms']:.2f} -> "
        f"{current['p95_latency_ms']:.2f} ms"
        + (
            f" ({delta['p95_latency_pct']:+.2f}%)"
            if delta["p95_latency_pct"] is not None
            else ""
        )
    )
    print(
        f"Run duration: {baseline['duration_ms']:.0f} -> "
        f"{current['duration_ms']:.0f} ms ({delta['duration_ms']:+.0f} ms)"
    )

    if comparison["regressions"]:
        print(f"\nREGRESSIONS ({len(comparison['regressions'])})")
        for item in comparison["regressions"]:
            print(f"  ! {item['reason']}: {item.get('delivery', 'overall metric')}")
    else:
        print("\nNO REGRESSIONS DETECTED")

    if comparison["improvements"]:
        print(f"\nIMPROVEMENTS ({len(comparison['improvements'])})")
        for item in comparison["improvements"]:
            print(f"  + {item['reason']}: {item.get('delivery', 'overall metric')}")
    return 1 if comparison["has_regressions"] else 0


def _history_compare_command(args: argparse.Namespace) -> int:
    comparison = compare_history_runs(
        args.baseline_run_id,
        current_run_id=args.current,
        path=args.db,
        latency_threshold_pct=args.latency_threshold_pct,
        allow_config_change=args.allow_config_change,
    )
    return _print_history_comparison(comparison)


def _history_compare_file_command(args: argparse.Namespace) -> int:
    comparison = compare_history_file(
        args.baseline_file,
        current_run_id=args.current,
        path=args.db,
        latency_threshold_pct=args.latency_threshold_pct,
        allow_config_change=args.allow_config_change,
    )
    print(f"Baseline file: {Path(args.baseline_file).expanduser()}")
    return _print_history_comparison(comparison)


def _history_export_command(args: argparse.Namespace) -> int:
    output = export_history_run(args.run_id, args.output, args.db)
    print(f"Exported HookStorm baseline: {output}")
    run_id = args.run_id
    if run_id == "latest":
        run_id = list_runs(args.db, limit=1)[0]["run_id"]
    print(f"Run: {run_id}")
    return 0


def _prompt_text(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} ({default}): ").strip()
    except EOFError:
        return default
    return answer or default


def _prompt_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    choices_label = "/".join(choices)
    while True:
        answer = _prompt_text(f"{prompt} [{choices_label}]", default).lower()
        if answer in choices:
            return answer
        print(f"Choose one of: {choices_label}", file=sys.stderr)


def _prompt_integer(prompt: str, default: int, *, minimum: int, maximum: int) -> int:
    while True:
        answer = _prompt_text(f"{prompt} [{minimum}–{maximum}]", str(default))
        try:
            value = int(answer)
        except ValueError:
            print(f"Enter a whole number between {minimum} and {maximum}.", file=sys.stderr)
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Enter a whole number between {minimum} and {maximum}.", file=sys.stderr)


def _init_command(args: argparse.Namespace) -> int:
    interactive = sys.stdin.isatty()
    signature_provider = args.signature_provider
    if signature_provider is None and interactive:
        signature_provider = _prompt_choice(
            "Signature provider",
            ("generic", "stripe", "github", "shopify", "standard_webhooks"),
            "generic",
        )
    signature_provider = signature_provider or "generic"

    event_type = args.event_type
    if event_type is None and interactive:
        event_type = _prompt_text("Event type", "test.ping")
    event_type = event_type or "test.ping"

    event_id = args.event_id
    if event_id is None and interactive:
        event_id = _prompt_text("Event ID", "evt_hookstorm_init_001")
    event_id = event_id or "evt_hookstorm_init_001"

    max_attempts = args.max_attempts
    if max_attempts is None and interactive:
        max_attempts = _prompt_integer(
            "Maximum attempts per delivery", 1, minimum=1, maximum=10
        )
    max_attempts = 1 if max_attempts is None else max_attempts

    duplicate_count = args.duplicate_count
    if duplicate_count is None and interactive:
        duplicate_count = _prompt_integer(
            "Additional duplicate deliveries", 0, minimum=0, maximum=100
        )
    duplicate_count = 0 if duplicate_count is None else duplicate_count

    written = initialize_project(
        scenario_path=args.scenario_path,
        workflow_path=args.workflow_path,
        event_type=event_type,
        event_id=event_id,
        signature_provider=signature_provider,
        max_attempts=max_attempts,
        duplicate_count=duplicate_count,
        include_workflow=not args.no_workflow,
        force=args.force,
    )
    print("HookStorm project files created:")
    current_directory = Path.cwd().resolve()
    for path in written:
        try:
            display_path = path.relative_to(current_directory)
        except ValueError:
            # Windows may report the current directory through an 8.3 short path
            # while resolved output paths use the long path spelling.
            display_path = path
        print(f"  + {display_path}")
    print("\nNext steps:")
    print(f"  1. Edit {args.scenario_path}: replace the sample payload and expected response.")
    print("  2. Configure GitHub Environment 'staging':")
    print("     Variable: HOOKSTORM_TARGET_URL")
    print("     Secret:   HOOKSTORM_SECRET")
    print("     Add required reviewers to protect staging credentials.")
    print("  3. Set HOOKSTORM_TARGET_URL and, for signed scenarios, HOOKSTORM_SECRET locally:")
    print(f"     hookstorm doctor {args.scenario_path}")
    print("     Add --connect for a socket check, or --verbose for DNS/IP timing and TLS certificate details.")
    if not args.no_workflow:
        print(f"  4. Commit {args.workflow_path} and {args.scenario_path}.")
        print("  5. Run the workflow on main once to create its first baseline.")
    else:
        print("  4. Add a CI workflow when you are ready to gate pull requests.")
    print("No target URL or secret was written to the generated files.")
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    if not 0.1 <= args.timeout <= 30:
        print("HookStorm doctor error: --timeout must be between 0.1 and 30 seconds", file=sys.stderr)
        return 2
    network_mode = "verbose" if args.verbose else "connect" if args.connect else "none"
    checks = run_preflight(
        args.scenario,
        target=args.target,
        secret_env=args.secret_env,
        connect=args.connect or args.verbose,
        verbose=args.verbose,
        timeout=args.timeout,
        allow_insecure_http=args.allow_insecure_http,
    )
    document = build_preflight_document(
        checks,
        args.scenario,
        network_mode=network_mode,
    )
    if args.format == "json":
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    else:
        print(f"HookStorm doctor · {args.scenario}")
        for check in checks:
            mark = {"PASS": "✓", "FAIL": "✗", "WARN": "!", "INFO": "i"}[check.status]
            print(f" {mark} {check.label}: {check.message}")
            if args.verbose:
                for detail in check.details:
                    print(f"    {detail}")
        print("\nPreflight: " + ("NOT READY" if not document["ready"] else "READY"))
    return document["exit_code"]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            return _init_command(args)
        if args.command == "doctor":
            return _doctor_command(args)
        if args.command == "history":
            if args.history_command == "list":
                return _history_list_command(args)
            if args.history_command == "show":
                return _history_show_command(args)
            if args.history_command == "compare":
                return _history_compare_command(args)
            if args.history_command == "compare-file":
                return _history_compare_file_command(args)
            if args.history_command == "export":
                return _history_export_command(args)

        scenario = load_scenario(args.scenario)
        if args.command == "validate":
            print(f"Scenario valid: {scenario['name']} ({len(scenario['events'])} event(s))")
            return 0
        if args.command == "export-pytest":
            output = write_pytest(scenario, args.output)
            print(f"pytest integration test written: {output.resolve()}")
            print("Set HOOKSTORM_TARGET to the test receiver URL before running pytest.")
            return 0
        secret = os.getenv(args.secret_env)
        result = asyncio.run(
            run_scenario(
                scenario,
                args.target,
                allow_remote=args.allow_remote,
                allow_insecure_http=args.allow_insecure_http,
                secret=secret,
            )
        )

        history_id = None
        if not args.no_history:
            try:
                history_id = save_run(result, args.history_db)
            except HistoryError as exc:
                print(f"History warning: {exc}", file=sys.stderr)

        if args.format == "json" and not args.output:
            sys.stdout.write(render_json(result))
            if history_id:
                print(f"History: {history_id}", file=sys.stderr)
        else:
            default_names = {
                "html": "hookstorm-report.html",
                "json": "hookstorm-report.json",
                "junit": "hookstorm-report.xml",
            }
            output = Path(args.output or default_names[args.format])
            write_report(result, output, args.format)
            _print_summary(result)
            print(f"Report: {output.resolve()}")
            if history_id:
                print(f"History: {history_id}")
            elif args.no_history:
                print("History: disabled for this run")
        return 0 if result["summary"]["success"] else 1
    except (HistoryError, ValueError, OSError) as exc:
        print(f"HookStorm error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
