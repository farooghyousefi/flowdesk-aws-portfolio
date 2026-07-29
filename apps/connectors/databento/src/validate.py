from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from .config import (
    DATASET,
    DEFAULT_SYMBOL,
    SCHEMA,
    ConnectorError,
    announce_data_file_selection,
    resolve_data_file,
    safe_error,
)
from .dbn_reader import DbnSummary, summarize_dbn


def validate_summary(
    summary: DbnSummary,
    *,
    expected_symbols: Iterable[str] | None = None,
    expected_instrument_id: int | None = None,
) -> list[str]:
    """Validate an MES MBO file without assuming one specific output symbology.

    Databento DBN metadata reflects ``stype_out``. A request submitted with
    ``stype_out=instrument_id`` can therefore expose ``"42003239"`` instead of
    the input continuous symbol ``"MES.v.0"``. The event records and the
    request manifest remain the source of truth for the instrument identity.
    """
    errors: list[str] = []
    if summary.dataset != DATASET:
        errors.append(f"Unexpected dataset: {summary.dataset or 'missing'}")
    if summary.schema != SCHEMA:
        errors.append(f"Unexpected schema: {summary.schema or 'missing'}")
    if summary.record_count <= 0:
        errors.append("No MBO records were found.")
    if not summary.instrument_ids or any(value <= 0 for value in summary.instrument_ids):
        errors.append("A valid instrument ID is required.")
    if expected_instrument_id is not None and summary.instrument_ids != [expected_instrument_id]:
        rendered = ", ".join(map(str, summary.instrument_ids)) or "missing"
        errors.append(
            f"Instrument ID mismatch: expected {expected_instrument_id}, found {rendered}."
        )
    if not summary.first_timestamp or not summary.last_timestamp:
        errors.append("Event timestamps are required.")

    explicit_identity = expected_symbols is not None or expected_instrument_id is not None
    accepted_symbols = {
        str(value).strip()
        for value in (expected_symbols or (DEFAULT_SYMBOL,))
        if str(value).strip()
    }
    if expected_instrument_id is not None:
        accepted_symbols.add(str(expected_instrument_id))

    if not summary.raw_symbols:
        errors.append("DBN symbol metadata is required.")
    elif not any(symbol in accepted_symbols for symbol in summary.raw_symbols):
        expected = ", ".join(sorted(accepted_symbols)) or DEFAULT_SYMBOL
        errors.append(f"The DBN metadata does not identify the expected MES instrument ({expected}).")

    if explicit_identity:
        foreign_symbols = [symbol for symbol in summary.raw_symbols if symbol not in accepted_symbols]
    else:
        # Preserve the standalone validator's historical tolerance for other
        # MES contract symbols while still rejecting unrelated products.
        foreign_symbols = [
            symbol
            for symbol in summary.raw_symbols
            if symbol != DEFAULT_SYMBOL and not symbol.startswith("MES")
        ]
    if foreign_symbols:
        errors.append(f"Foreign products found: {', '.join(foreign_symbols)}")

    unknown_actions = sorted(set(summary.action_counts) - {"A", "C", "M", "T", "F", "R", "N"})
    if unknown_actions:
        errors.append(f"Unknown MBO actions found: {', '.join(unknown_actions)}")
    return errors


def print_validation(summary: DbnSummary, errors: list[str]) -> None:
    counts = summary.action_counts
    print("DATABENTO FILE VALIDATION")
    print()
    print(f"File: {summary.file}")
    print(f"Schema: {summary.schema}")
    print(f"Records: {summary.record_count}")
    print(f"First timestamp: {summary.first_timestamp or '-'}")
    print(f"Last timestamp: {summary.last_timestamp or '-'}")
    print(f"Instrument IDs: {', '.join(map(str, summary.instrument_ids)) or '-'}")
    print(f"Raw symbols: {', '.join(summary.raw_symbols) or '-'}")
    print(f"Add events: {counts.get('A', 0)}")
    print(f"Cancel events: {counts.get('C', 0)}")
    print(f"Modify events: {counts.get('M', 0)}")
    print(f"Trade events: {counts.get('T', 0)}")
    print(f"Clear events: {counts.get('R', 0)}")
    print(f"Validation: {'FAILED' if errors else 'PASSED'}")
    for error in errors:
        print(f"- {error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Databento MBO DBN file.")
    files = parser.add_mutually_exclusive_group()
    files.add_argument("--file")
    files.add_argument("--latest", action="store_true")
    return parser.parse_args(argv)


def validate_file(path: Path) -> tuple[DbnSummary, list[str]]:
    summary = summarize_dbn(path)
    return summary, validate_summary(summary)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        path = resolve_data_file(args.file, latest=args.latest)
        announce_data_file_selection(path, file_arg=args.file, latest=args.latest)
        summary, errors = validate_file(path)
        print_validation(summary, errors)
        return 1 if errors else 0
    except Exception as exc:
        print(f"ERROR: {safe_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
