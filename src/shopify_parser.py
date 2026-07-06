"""
shopify_parser.py

Parses Shopify customer CSV exports into normalized data structures that
migration_utils.py can consume regardless of whether data came from CSV or
the GraphQL API.

Expected CSV export (from Shopify Admin > Customers > Export):
  - customers_export.csv
"""

import csv
import logging


def parse_customers(file_paths: list[str]) -> list[dict]:
    """
    Parse one or more customers_export.csv files.

    Shopify caps customer CSV exports at 15 MB. For larger stores, export
    multiple files and pass them all here — duplicates are deduplicated by email,
    phone, or Customer ID (for accounts with no contact info).

    Returns a list of normalized customer dicts:
    {
        "shopify_customer_id": str,
        "email": str | None,
        "phone": str | None,
        "given_name": str | None,
        "family_name": str | None,
        "total_spent": str,
        "total_orders": str,
        "tags": str,
        "note": str,
    }
    """
    seen_keys: set[str] = set()
    customers = []

    for file_path in file_paths:
        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                email = row.get("Email", "").strip() or None
                phone = row.get("Phone", "").strip() or None

                # Deduplicate across multiple files. Customers with no email or phone
                # are kept — migration_utils handles them with a placeholder login ID.
                dedup_key = email or phone or row.get("Customer ID", "").strip()
                if not dedup_key:
                    logging.warning(
                        f"Skipping row in {file_path} with no email, phone, or Customer ID — "
                        "cannot deduplicate or create a login ID."
                    )
                    continue
                if dedup_key in seen_keys:
                    logging.info(
                        f"Skipping duplicate customer {dedup_key} found in {file_path}."
                    )
                    continue
                seen_keys.add(dedup_key)

                customers.append(
                    {
                        "shopify_customer_id": row.get("Customer ID", "").strip(),
                        "email": email,
                        "phone": phone,
                        "given_name": row.get("First Name", "").strip() or None,
                        "family_name": row.get("Last Name", "").strip() or None,
                        "total_spent": row.get("Total Spent", "0.00").strip(),
                        "total_orders": row.get("Total Orders", "0").strip(),
                        "tags": row.get("Tags", "").strip(),
                        "note": row.get("Note", "").strip(),
                    }
                )

    logging.info(f"Parsed {len(customers)} unique customers from {len(file_paths)} file(s).")
    return customers
