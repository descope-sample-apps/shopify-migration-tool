"""
main.py

Entry point for the Shopify → Descope customer migration tool.

Usage:
  # From CSV (single file)
  python3 src/main.py --customers customers_export.csv

  # From CSV (multiple files — workaround for Shopify's 15 MB export cap)
  python3 src/main.py --customers customers_1.csv customers_2.csv

  # From Shopify GraphQL API
  python3 src/main.py --from-api

  # Dry run (no changes made to Descope)
  python3 src/main.py --customers customers_export.csv --dry-run

  # Verbose output
  python3 src/main.py --from-api --verbose
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

from shopify_parser import parse_customers
from shopify_client import fetch_customers
from migration_utils import (
    ensure_custom_attributes,
    process_customers,
)


_NEEDS_CONTACT_CSV = "needs_contact_info.csv"


def _write_needs_contact_csv(placeholders: list[dict]) -> None:
    """Write placeholder customer accounts to a CSV for manual follow-up."""
    fieldnames = ["shopify_id", "given_name", "family_name", "placeholder_login_id"]
    file_exists = os.path.isfile(_NEEDS_CONTACT_CSV)
    with open(_NEEDS_CONTACT_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(placeholders)
    print(f"  → Written to {_NEEDS_CONTACT_CSV}")


def _print_summary(customer_result: dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""

    print()
    print("=" * 60)
    print(f"{prefix}MIGRATION SUMMARY")
    print("=" * 60)
    print(f"\n── Customers ────────────────────────────────────────────")
    print(f"  Total processed       : {customer_result['total']}")
    print(f"  Created               : {customer_result['created']}")
    print(f"  Merged into existing  : {customer_result['merged']}")
    print(f"  Placeholder accounts  : {len(customer_result['placeholders'])}")
    print(f"  Failed                : {len(customer_result['failed'])}")
    if customer_result["placeholders"] and not dry_run:
        print(
            f"  ⚠ {len(customer_result['placeholders'])} customer(s) had no contact info "
            "— placeholder login IDs assigned."
        )
    if customer_result["failed"]:
        print("  Failed customers:")
        for c in customer_result["failed"]:
            print(f"    - {c}")

    if customer_result["placeholders"] and not dry_run:
        print()
        print(
            f"  {len(customer_result['placeholders'])} account(s) need contact info added "
            f"— writing {_NEEDS_CONTACT_CSV}..."
        )
        _write_needs_contact_csv(customer_result["placeholders"])

    print()
    if not dry_run:
        print("Migration complete. Full log written to logs/")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate Shopify customers to Descope.\n\n"
            "Provide either --customers (one or more CSV files) or --from-api\n"
            "(fetches directly from the Shopify GraphQL API).\n\n"
            "Note: Shopify caps customer CSV exports at 15 MB. For large stores,\n"
            "export multiple files using segment or date filters and pass them all\n"
            "via --customers, or use --from-api to paginate automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Customer source (mutually exclusive, one required) ────────────────────
    customer_source = parser.add_mutually_exclusive_group(required=True)
    customer_source.add_argument(
        "--customers",
        nargs="+",
        metavar="FILE",
        help="Path(s) to customers_export.csv file(s). Mutually exclusive with --from-api.",
    )
    customer_source.add_argument(
        "--from-api",
        action="store_true",
        help=(
            "Fetch customers from Shopify GraphQL API. "
            "Requires SHOPIFY_SHOP_URL and SHOPIFY_ACCESS_TOKEN in .env "
            "(or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET to obtain a token via OAuth). "
            "Mutually exclusive with --customers."
        ),
    )

    # ── Behaviour flags ───────────────────────────────────────────────────────
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without making any changes.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each customer as it is processed.",
    )

    args = parser.parse_args()
    dry_run: bool = args.dry_run
    verbose: bool = args.verbose

    if dry_run:
        print("Running in DRY RUN mode — no changes will be made to Descope.\n")

    # ── Load data ─────────────────────────────────────────────────────────────

    if args.from_api:
        print("Fetching customers from Shopify GraphQL API...")
        customers = fetch_customers()
    else:
        print(f"Loading customers from {len(args.customers)} CSV file(s)...")
        customers = parse_customers(args.customers)

    print(f"\nLoaded: {len(customers)} customers.\n")

    # ── Ensure prerequisites exist in Descope ─────────────────────────────────

    if not dry_run:
        print("Ensuring custom attributes exist in Descope...")
        ensure_custom_attributes()

    # ── Migration ─────────────────────────────────────────────────────────────

    customer_result = process_customers(customers, dry_run, verbose)

    # ── Summary ───────────────────────────────────────────────────────────────

    _print_summary(customer_result, dry_run)

    if customer_result["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
