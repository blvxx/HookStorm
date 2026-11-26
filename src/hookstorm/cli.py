"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from hookstorm import __version__
from hookstorm.config import ScenarioError, load_scenario
from hookstorm.report import render_json, write_report
from hookstorm.runner import run_scenario
from hookstorm.safety import UnsafeTargetError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hookstorm",
        description="Test webhook delivery behavior with repeatable failure scenarios.",
        epilog="Remote targets are blocked by default. Use --allow-remote only when authorized.",
    )
    parser.add_argument("--version", action="version", version=f"HookStorm {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="execute a YAML scenario against a webhook endpoint")
    run.add_argument("scenario", help="path to a scenario YAML file")
    run.add_argument("--target", required=True, help="webhook URL; localhost only unless opted in")
    run.add_argument("--allow-remote", action="store_true", help="allow a non-local target")
    run.add_argument("--secret-env", default="HOOKSTORM_SECRET", help="environment variable for HMAC secret")
    run.add_argument("--format", choices=("html", "json"), default="html", help="report format")
    run.add_argument("--output", help="write a report to this path")

    validate = subparsers.add_parser("validate", help="validate a scenario without sending requests")
    validate.add_argument("scenario", help="path to a scenario YAML file")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
        if args.command == "validate":
            print(f"Scenario valid: {scenario['name']} ({len(scenario['events'])} event(s))")
            return 0
        secret = os.getenv(args.secret_env)
        result = asyncio.run(
            run_scenario(scenario, args.target, allow_remote=args.allow_remote, secret=secret)
        )
        if args.format == "json" and not args.output:
            sys.stdout.write(render_json(result))
        else:
            output = Path(args.output or f"hookstorm-report.{args.format}")
            write_report(result, output, args.format)
            _print_summary(result)
            print(f"Report: {output.resolve()}")
        return 0 if result["summary"]["success"] else 1
    except ValueError as exc:
        print(f"HookStorm error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
