"""
shopify_parser.py

Parses Shopify CSV exports into normalized data structures that migration_utils.py
can consume regardless of whether data came from CSV or the GraphQL API.

Expected CSV exports (from Shopify Admin > Settings > Export):
  - customers_export.csv
  - users_export.csv   (staff)
  - roles_export.csv
"""

import csv
import logging
from collections import defaultdict


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


def parse_staff_users(file_path: str) -> list[dict]:
    """
    Parse users_export.csv (staff users).

    A single staff member appears once per role assignment, so this function
    groups rows by email and aggregates roles.

    Returns a list of normalized staff dicts:
    {
        "shopify_user_id": str,
        "email": str | None,
        "phone": str | None,
        "given_name": str | None,
        "family_name": str | None,
        "user_type": str,         # e.g. "Admin", "Point of sale", "Collaborator"
        "status": str,            # "Active", "Pending" (invite not accepted), or "Inactive"
        "store_name": str,
        "roles": [str, ...],      # all role names assigned to this user
    }
    """
    # email -> dict (first row wins for scalar fields; roles are accumulated)
    users_by_email: dict[str, dict] = {}

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("email", "").strip() or None
            # Note: staff with no email AND no phone are kept — migration_utils
            # handles them by creating a placeholder login ID in Descope.

            role = row.get("role_name", "").strip()
            phone = row.get("phone", "").strip() or None
            shopify_user_id = row.get("id", "").strip()

            # Use email as the grouping key; fall back to shopify_user_id for
            # the rare case where a staff account has no email at all.
            group_key = email or shopify_user_id

            if group_key not in users_by_email:
                users_by_email[group_key] = {
                    "shopify_user_id": shopify_user_id,
                    "email": email,
                    "phone": phone,
                    "given_name": row.get("given_name", "").strip() or None,
                    "family_name": row.get("family_name", "").strip() or None,
                    "user_type": row.get("user_type", "").strip(),
                    "status": row.get("status", "Active").strip(),
                    "store_name": row.get("store_name", "").strip(),
                    "roles": [role] if role else [],
                }
            else:
                # Accumulate additional roles for the same user
                if role and role not in users_by_email[group_key]["roles"]:
                    users_by_email[group_key]["roles"].append(role)

    staff = list(users_by_email.values())
    logging.info(f"Parsed {len(staff)} unique staff users from {file_path}.")
    return staff


def parse_roles(file_path: str) -> list[dict]:
    """
    Parse roles_export.csv.

    Each row is one role+permission pair. Groups by role name and aggregates
    all permissions per role.

    Returns a list of normalized role dicts:
    {
        "name": str,
        "category": str,          # "Store" or "Point of sale"
        "permissions": [str, ...],
    }
    """
    roles_by_name: dict[str, dict] = {}

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name", "").strip()
            if not name:
                continue

            permission = row.get("permission", "").strip()

            if name not in roles_by_name:
                roles_by_name[name] = {
                    "name": name,
                    "category": row.get("category", "").strip(),
                    "permissions": [permission] if permission else [],
                }
            else:
                if permission and permission not in roles_by_name[name]["permissions"]:
                    roles_by_name[name]["permissions"].append(permission)

    roles = list(roles_by_name.values())
    logging.info(f"Parsed {len(roles)} unique roles from {file_path}.")
    return roles
