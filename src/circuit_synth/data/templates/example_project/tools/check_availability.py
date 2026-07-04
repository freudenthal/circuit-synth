#!/usr/bin/env python3
"""Check real component availability across suppliers (DigiKey / JLCPCB).

Usage:
    uv run python tools/check_availability.py "<query>" [--source digikey|jlcpcb]
                                              [--min-stock N] [--max-results N]

Prints an aligned table of *real* availability rows and a ``skipped:`` line for
any source that could not be queried (missing credentials, network error). It
never fabricates stock or prices and never uses the JLCPCB demo scraper.

Credentials (optional -- without them the relevant source is simply skipped):
  DigiKey: DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET (or cs-setup-digikey-api)
  JLCPCB:  JLCPCB_KEY / JLCPCB_SECRET

Always exits 0 -- this is an informational tool; "everything skipped" is a
valid, honest outcome, not a failure.
"""

import argparse
import sys

from circuit_synth.manufacturing import check_availability


def main() -> int:
    ap = argparse.ArgumentParser(description="Check component availability.")
    ap.add_argument("query", help="MPN or free-text part query, e.g. '2N7000'")
    ap.add_argument(
        "--source",
        action="append",
        choices=["digikey", "jlcpcb"],
        help="Limit to a source (repeatable); default tries all.",
    )
    ap.add_argument("--min-stock", type=int, default=0)
    ap.add_argument("--max-results", type=int, default=5)
    args = ap.parse_args()

    sources = tuple(args.source) if args.source else ("digikey", "jlcpcb")
    report = check_availability(
        args.query,
        sources=sources,
        min_stock=args.min_stock,
        max_results=args.max_results,
    )

    if report.results:
        header = f"{'SOURCE':<9} {'MPN':<24} {'STOCK':>10} {'PRICE':>10}  DATASHEET"
        print(header)
        print("-" * len(header))
        for r in report.results:
            price = f"{r.unit_price:.4f}" if r.unit_price is not None else "n/a"
            print(
                f"{r.source:<9} {r.mpn:<24} {r.stock:>10} {price:>10}  "
                f"{r.datasheet_url or ''}"
            )
    else:
        print(f"No real availability found for '{args.query}'.")

    for source, reason in report.skipped.items():
        print(f"skipped: {source} -- {reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
