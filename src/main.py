"""
main.py

Entry point for the Shopify → Descope migration tool.

Staff and customer migration are each optional. Pass the relevant arguments to
select which populations to migrate. At least one must be selected.

Usage:
  # Staff only, with roles
  python3 src/main.py --users users_export.csv --roles roles_export.csv

  # Staff only, without roles (staff tagged with "Staff" role but no Shopify roles assigned in Descope)
  python3 src/main.py --users users_export.csv

  # Customers only, from CSV
  python3 src/main.py --customers customers_export.csv

  # Customers only, from API
  python3 src/main.py --from-api

  # Both — customers from CSV
  python3 src/main.py --customers customers_export.csv \
                      --users users_export.csv \
                      --roles roles_export.csv

  # Both — customers from API, multiple customer CSV files
  python3 src/main.py --customers customers_1.csv customers_2.csv \
                      --users users_export.csv \
                      --roles roles_export.csv

  # Dry run (no changes made to Descope)
  python3 src/main.py --from-api --users users.csv --roles roles.csv --dry-run

  # Verbose output
  python3 src/main.py ... --verbose
"""

from __future__ import annotations

import argparse
import csv
import sys

from shopify_parser import parse_customers, parse_staff_users, parse_roles
from shopify_client import fetch_customers
from migration_utils import (
    ensure_custom_attributes,
    ensure_base_roles,
    process_roles,
    process_staff_users,
    process_customers,
)


_NEEDS_CONTACT_CSV = "needs_contact_info.csv"


def _write_needs_contact_csv(placeholders: list[dict]) -> None:
    """Write placeholder accounts (staff and/or customer) to a CSV for manual follow-up."""
    fieldnames = ["user_type", "shopify_id", "given_name", "family_name", "roles", "placeholder_login_id"]
    with open(_NEEDS_CONTACT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(placeholders)
    print(f"  → Written to {_NEEDS_CONTACT_CSV}")


def _print_summary(
    role_result: dict | None,
    staff_result: dict | None,
    customer_result: dict | None,
    dry_run: bool,
) -> None:
    prefix = "[DRY RUN] " if dry_run else ""

    print()
    print("=" * 60)
    print(f"{prefix}MIGRATION SUMMARY")
    print("=" * 60)

    if role_result is not None:
        print("\n── Roles & Permissions ──────────────────────────────────")
        print(f"  Total roles processed : {role_result['total']}")
        print(f"  Created               : {role_result['created']}")
        print(f"  Already existed       : {role_result['skipped_existing']}")
        print(f"  Failed                : {len(role_result['failed'])}")
        print(f"  Permissions created   : {role_result['permissions_created']}")
        print(f"  Permissions skipped   : {role_result['permissions_skipped']}")
        print(f"  Permissions failed    : {len(role_result['permissions_failed'])}")
        if role_result["failed"]:
            print("  Failed roles:")
            for r in role_result["failed"]:
                print(f"    - {r}")
        if role_result["permissions_failed"]:
            print("  Failed permissions:")
            for p in role_result["permissions_failed"]:
                print(f"    - {p}")

    if staff_result is not None:
        print("\n── Staff Users ──────────────────────────────────────────")
        print(f"  Total staff processed   : {staff_result['total']}")
        print(f"  Created                 : {staff_result['created']}")
        print(f"  Already existed         : {staff_result['skipped_existing']}")
        print(f"  Collaborators skipped   : {staff_result['skipped_collaborators']}")
        print(f"  Placeholder accounts    : {len(staff_result['placeholders'])}")
        print(f"  Failed                  : {len(staff_result['failed'])}")
        print(f"  Role assignments failed : {len(staff_result['roles_failed'])}")
        if staff_result["placeholders"] and not dry_run:
            print(f"  ⚠ {len(staff_result['placeholders'])} staff account(s) had no contact info — placeholder login IDs assigned.")
        if staff_result["failed"]:
            print("  Failed users:")
            for u in staff_result["failed"]:
                print(f"    - {u}")
        if staff_result["roles_failed"]:
            print("  Failed role assignments:")
            for r in staff_result["roles_failed"]:
                print(f"    - {r}")

    if customer_result is not None:
        print("\n── Customers ────────────────────────────────────────────")
        print(f"  Total customers processed : {customer_result['total']}")
        print(f"  Created                   : {customer_result['created']}")
        print(f"  Merged into existing user : {customer_result['merged']}")
        print(f"  Placeholder accounts      : {len(customer_result['placeholders'])}")
        print(f"  Failed                    : {len(customer_result['failed'])}")
        if customer_result["placeholders"] and not dry_run:
            print(f"  ⚠ {len(customer_result['placeholders'])} customer(s) had no contact info — placeholder login IDs assigned.")
        if customer_result["failed"]:
            print("  Failed customers:")
            for c in customer_result["failed"]:
                print(f"    - {c}")

    # Write the combined needs_contact_info.csv once, after all phases.
    all_placeholders = (
        (staff_result["placeholders"] if staff_result else [])
        + (customer_result["placeholders"] if customer_result else [])
    )
    if all_placeholders and not dry_run:
        print()
        print(f"  {len(all_placeholders)} account(s) need contact info added — writing {_NEEDS_CONTACT_CSV}...")
        _write_needs_contact_csv(all_placeholders)

    print()
    if not dry_run:
        print("Migration complete. Full log written to logs/")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate Shopify staff users and/or customers to Descope.\n\n"
            "Staff and customer migration are each optional — pass the relevant\n"
            "arguments to select which populations to migrate. At least one must\n"
            "be selected. --roles is optional even when migrating staff; omit it\n"
            "to skip Shopify role migration and assign roles manually in Descope.\n"
            "--roles cannot be used without --users.\n\n"
            "Note: Shopify caps customer CSV exports at 15 MB. For large stores,\n"
            "export multiple files using segment or date filters and pass them all\n"
            "via --customers, or use --from-api to paginate automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Customer source (mutually exclusive, both optional) ──────────────────
    customer_source = parser.add_mutually_exclusive_group()
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
            "Requires SHOPIFY_SHOP_URL and SHOPIFY_ACCESS_TOKEN in .env. "
            "Mutually exclusive with --customers."
        ),
    )

    # ── Staff source ─────────────────────────────────────────────────────────
    parser.add_argument(
        "--users",
        metavar="FILE",
        help=(
            "Path to users_export.csv (staff users). "
            "If --roles is omitted, staff accounts are created with no roles assigned."
        ),
    )
    parser.add_argument(
        "--roles",
        metavar="FILE",
        help=(
            "Path to roles_export.csv. Optional even when --users is provided — "
            "omit if you prefer to assign roles manually in Descope. "
            "Cannot be used without --users."
        ),
    )

    # ── Tenant ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--tenant",
        metavar="TENANT_ID",
        help=(
            "Descope tenant ID. When provided, roles are assigned at tenant level "
            "using add_tenant_roles / AssociatedTenant. "
            "When omitted, roles are assigned at project level. "
            "Roles and permissions are always created at project level regardless."
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
        help="Print each user/role as it is processed.",
    )

    args = parser.parse_args()

    # ── Validate argument combinations ────────────────────────────────────────
    if args.roles and not args.users:
        parser.error("--roles requires --users (roles only apply to staff users).")

    migrate_staff = bool(args.users)
    migrate_customers = bool(args.customers or args.from_api)

    if not migrate_staff and not migrate_customers:
        parser.error(
            "Nothing to migrate. Provide --users/--roles for staff, "
            "--customers or --from-api for customers, or both."
        )

    dry_run: bool = args.dry_run
    verbose: bool = args.verbose
    tenant_id: str | None = args.tenant or None

    if dry_run:
        mode = f"tenant '{tenant_id}'" if tenant_id else "project level"
        print(f"Running in DRY RUN mode — no changes will be made to Descope. (role assignment: {mode})\n")

    # ── Load data ─────────────────────────────────────────────────────────────

    roles, staff, customers = [], [], []

    if migrate_staff:
        if args.roles:
            print("Loading roles from CSV...")
            roles = parse_roles(args.roles)
        else:
            print("No --roles provided — staff accounts will be created with no roles assigned.")
        print("Loading staff users from CSV...")
        staff = parse_staff_users(args.users)

    if migrate_customers:
        if args.from_api:
            print("Fetching customers from Shopify GraphQL API...")
            customers = fetch_customers()
        else:
            print(f"Loading customers from {len(args.customers)} CSV file(s)...")
            customers = parse_customers(args.customers)

    parts = []
    if migrate_staff:
        parts.append(f"{len(roles)} roles, {len(staff)} staff users")
    if migrate_customers:
        parts.append(f"{len(customers)} customers")
    print(f"\nLoaded: {', '.join(parts)}.\n")

    # ── Ensure custom attributes exist ────────────────────────────────────────

    if not dry_run:
        print("Ensuring custom attributes exist in Descope...")
        ensure_custom_attributes()
        print("Ensuring base tagging roles exist in Descope...")
        ensure_base_roles(migrate_staff=migrate_staff, migrate_customers=migrate_customers)

    # ── Migration phases ──────────────────────────────────────────────────────

    role_result = None
    staff_result = None
    customer_result = None

    if migrate_staff:
        # Roles must be created before users so role assignments don't fail.
        role_result = process_roles(roles, dry_run, verbose)
        staff_result = process_staff_users(staff, dry_run, verbose, tenant_id=tenant_id)

    if migrate_customers:
        customer_result = process_customers(customers, dry_run, verbose, tenant_id=tenant_id)

    # ── Summary ───────────────────────────────────────────────────────────────

    _print_summary(role_result, staff_result, customer_result, dry_run)

    # Exit non-zero if any failures occurred
    failures = (
        len(role_result["failed"]) + len(role_result["permissions_failed"])
        if role_result else 0
    ) + (
        len(staff_result["failed"]) if staff_result else 0
    ) + (
        len(customer_result["failed"]) if customer_result else 0
    )
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
