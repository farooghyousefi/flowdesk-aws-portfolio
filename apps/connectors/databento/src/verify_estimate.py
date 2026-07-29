from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

import databento as db

from .config import (
    DEFAULT_VERIFICATION_LIMIT,
    ConnectorConfig,
    ConnectorError,
    HistoricalRequest,
    announce_data_file_selection,
    load_config,
    resolve_data_file,
    safe_error,
    validate_verification_limit,
)
from .estimate import is_request_allowed, save_receipt
from .verification_context import (
    VerificationEstimate,
    build_reference_request,
    estimate_with_fallback,
    has_cost_precision_warning,
    inspect_mbo_verification_context,
    resolve_contract,
    resolve_verification_window,
)


def confirmed_command(mbo_path: Path, request: HistoricalRequest) -> str:
    values = [
        "npm",
        "run",
        "databento:verify:book",
        "--",
        "--mbo-file",
        str(mbo_path),
        "--start",
        request.start_iso,
        "--end",
        request.end_iso,
        "--limit",
        str(request.limit),
        "--confirm",
    ]
    return " ".join(shlex.quote(value) for value in values)


def print_verification_estimate(
    estimate: VerificationEstimate,
    config: ConnectorConfig,
) -> None:
    request = estimate.request
    warning = has_cost_precision_warning(request)
    print("DATABENTO MBP-10 VERIFICATION ESTIMATE")
    print()
    print(f"Dataset: {request.dataset}")
    print(f"Schema: {request.schema}")
    print(f"Continuous input symbol: MES.v.0")
    print(f"Input symbol: {request.symbol}")
    print(f"Input symbology: {request.stype_in}")
    print(f"Resolved instrument ID: {request.instrument_id}")
    print(f"Resolved raw symbol: {request.raw_symbol}")
    print(f"Start: {request.start_iso}")
    print(f"End: {request.end_iso}")
    print(f"Record limit: {request.limit}")
    print(f"Estimated billable bytes: {estimate.billable_bytes}")
    print(f"Estimated billable MiB: {estimate.billable_mib:.6f}")
    print(f"Relevant unit price: {estimate.unit_price_usd_per_gb:.6f} USD/GB")
    print(f"Estimated cost USD: {estimate.estimated_cost_usd:.6f}")
    print(f"Max request cost USD: {config.max_request_cost_usd:.2f}")
    print(f"Max daily cost USD: {config.max_daily_cost_usd:.2f}")
    print(
        "Allowed: "
        f"{'YES' if is_request_allowed(estimate.estimated_cost_usd, config) else 'NO'}"
    )
    print(f"Cost estimate precision warning: {'YES' if warning else 'NO'}")
    if warning:
        print("WARNING: Databento get_cost may over-report for non-10-minute intervals.")
        print("The record limit remains the hard safety boundary.")


def persist_estimate(estimate: VerificationEstimate, config: ConnectorConfig) -> None:
    save_receipt(
        estimate.request,
        estimate.estimated_cost_usd,
        config,
        billable_bytes=estimate.billable_bytes,
        unit_price_usd_per_gb=estimate.unit_price_usd_per_gb,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate a bounded concrete-instrument MBP-10 verification sample."
    )
    files = parser.add_mutually_exclusive_group()
    files.add_argument("--mbo-file")
    files.add_argument("--latest", action="store_true")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--limit", type=int, default=DEFAULT_VERIFICATION_LIMIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    config: ConnectorConfig | None = None
    try:
        args = parse_args(argv)
        limit = validate_verification_limit(args.limit)
        mbo_path = resolve_data_file(args.mbo_file, latest=args.latest)
        announce_data_file_selection(mbo_path, file_arg=args.mbo_file, latest=args.latest)
        context = inspect_mbo_verification_context(mbo_path)
        start_ns, end_ns = resolve_verification_window(context, args.start, args.end)

        config = load_config()
        client = db.Historical(config.api_key)
        contract = resolve_contract(client, context, start_ns, end_ns)
        request = build_reference_request(contract, start_ns, end_ns, limit)
        primary, fallback = estimate_with_fallback(client, request, config)
        persist_estimate(primary, config)
        print_verification_estimate(primary, config)

        selected = primary
        if fallback is not None:
            persist_estimate(fallback, config)
            print()
            print("FALLBACK ESTIMATE: 100 RECORDS")
            print()
            print_verification_estimate(fallback, config)
            selected = fallback

        print()
        print("No MBP-10 time-series data was downloaded.")
        if is_request_allowed(selected.estimated_cost_usd, config):
            print(f"Confirmed download command: {confirmed_command(mbo_path, selected.request)}")
        else:
            print("Confirmed download command: BLOCKED by the configured request limit.")
        return 0
    except Exception as exc:
        secrets = (config.api_key,) if config else ()
        print(f"ERROR: {safe_error(exc, secrets)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
