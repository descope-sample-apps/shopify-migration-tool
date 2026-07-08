"""
migration_utils.py

All Descope SDK interactions for the Shopify → Descope customer migration.

Custom attributes auto-created in Descope before migration begins:
  - shopify_customer_id        (String)   Shopify customer ID
  - shopify_total_spent        (String)   lifetime spend value
  - shopify_total_orders       (String)   total order count
  - shopify_tags               (String)   comma-separated Shopify tags
  - shopify_note               (String)   internal Shopify note (not customer-visible)

Customers with no email and no phone are skipped — Descope requires a login ID
and there is no safe placeholder that would let the customer authenticate later.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from descope import AuthException, DescopeClient

# ── Logging ──────────────────────────────────────────────────────────────────

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
dt_string = datetime.now().strftime("%d_%m_%Y_%H-%M-%S")
logging.basicConfig(
    filename=os.path.join(log_dir, f"migration_log_{dt_string}.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
# Suppress httpx's per-request INFO lines (e.g. "HTTP/1.1 400 Bad Request").
# These are noisy and misleading — expected 400s (already-exists) look like
# errors. Our own log messages cover the meaningful outcomes instead.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Environment ───────────────────────────────────────────────────────────────

load_dotenv()
DESCOPE_PROJECT_ID = os.getenv("DESCOPE_PROJECT_ID", "").strip()
DESCOPE_MANAGEMENT_KEY = os.getenv("DESCOPE_MANAGEMENT_KEY", "").strip()

# Lazy singleton — initialized on first use so the module can be imported in
# tests without live credentials (pure helper functions don't need the client).
_descope_client: DescopeClient | None = None


def _client() -> DescopeClient:
    """Return the Descope client, initializing it on first call."""
    global _descope_client
    if _descope_client is None:
        if not DESCOPE_PROJECT_ID or not DESCOPE_MANAGEMENT_KEY:
            logging.error("DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY must be set in .env.")
            sys.exit(1)
        try:
            _descope_client = DescopeClient(
                project_id=DESCOPE_PROJECT_ID,
                management_key=DESCOPE_MANAGEMENT_KEY,
            )
        except AuthException as e:
            logging.error(f"Failed to initialize Descope client: {e}")
            sys.exit(1)
    return _descope_client


# ── Custom attribute definitions ──────────────────────────────────────────────

_CUSTOM_ATTRIBUTES = {
    "shopify_customer_id": "String",
    "shopify_total_spent": "String",
    "shopify_total_orders": "String",
    "shopify_tags": "String",
    "shopify_note": "String",
}

_ATTR_TYPE_MAP = {"String": 1, "Number": 2, "Boolean": 3}

# Descope error codes
_ERR_USER_EXISTS = "E011001"  # user already exists (login ID taken)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api_post_with_retry(url: str, payload: dict, max_retries: int = 4) -> requests.Response | None:
    """POST to the Descope management REST API with rate-limit retry."""
    headers = {
        "Authorization": f"Bearer {DESCOPE_PROJECT_ID}:{DESCOPE_MANAGEMENT_KEY}",
        "Content-Type": "application/json",
    }
    retries = 0
    while retries <= max_retries:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code != 429:
                return resp
            wait = min(5 ** (retries + 1), 60)
            logging.info(f"Rate limited. Retrying in {wait}s...")
            time.sleep(wait)
            retries += 1
        except requests.exceptions.Timeout:
            wait = min(5 ** (retries + 1), 60)
            logging.warning(f"Timeout. Retrying in {wait}s... ({retries + 1}/{max_retries})")
            time.sleep(wait)
            retries += 1
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error: {e}")
            return None
    logging.error("Max retries reached.")
    return None


def _parse_error_code(error: AuthException) -> str:
    """Extract the Descope errorCode string from an AuthException."""
    try:
        return json.loads(error.error_message).get("errorCode", "")
    except Exception:
        return ""


# ── Phase 0: Custom attributes ────────────────────────────────────────────────

def ensure_custom_attributes() -> None:
    """
    Auto-create all required custom attributes in Descope, one at a time.
    Safe to call repeatedly — existing attributes are silently skipped.

    Note: the Descope Python SDK does not expose custom attribute management,
    so this function calls the REST API directly.
    """
    _ERR_ATTR_EXISTS = "E016002"

    for name, type_str in _CUSTOM_ATTRIBUTES.items():
        payload = {
            "attributes": [
                {
                    "name": name,
                    "type": _ATTR_TYPE_MAP.get(type_str, 1),
                    "options": [],
                    "displayName": name,
                    "defaultValue": {},
                    "viewPermissions": [],
                    "editPermissions": [],
                    "editable": True,
                }
            ]
        }
        resp = _api_post_with_retry(
            "https://api.descope.com/v1/mgmt/user/customattribute/create",
            payload,
        )
        if resp is None:
            logging.error(f"Custom attribute '{name}': no response from Descope.")
        elif resp.ok:
            logging.info(f"Custom attribute '{name}' created.")
        else:
            try:
                err_code = resp.json().get("errorCode", "")
            except Exception:
                err_code = ""
            if err_code == _ERR_ATTR_EXISTS:
                logging.info(f"Custom attribute '{name}' already exists — skipping.")
            else:
                logging.error(
                    f"Custom attribute '{name}' failed ({resp.status_code}): {resp.text[:200]}"
                )


# ── Migration: Customers ──────────────────────────────────────────────────────

def process_customers(customers: list[dict], dry_run: bool, verbose: bool) -> dict:
    """
    Create customer users in Descope. If a customer's email already exists in
    Descope (e.g. a pre-existing user), their Shopify attributes are merged in
    without touching the existing account's activation state.

    Merge behaviour:
      - Email match: the customer's Shopify attributes are patched onto the
        existing record, and their phone is added as an additional login ID if
        not already present.
      - create() fails with user-already-exists and no prior email match was
        found: searches by phone to locate the duplicate and applies the same
        merge logic (adding email as an additional login ID if not present).
      - Activation state is left untouched for merged users.

    Customers with no email and no phone are skipped — they cannot be given a
    meaningful login ID.

    Args:
        customers: Normalized customer list from shopify_parser or shopify_client
    """
    result = {
        "total": len(customers),
        "created": 0,
        "merged": 0,
        "skipped": 0,
        "failed": [],
    }

    if dry_run:
        no_contact = [c for c in customers if not c.get("email") and not c.get("phone")]
        result["skipped"] = len(no_contact)
        print(f"Would migrate {len(customers) - len(no_contact)} customers to Descope")
        if no_contact:
            print(f"Would skip {len(no_contact)} customer(s) — no email or phone")
        if verbose:
            for c in customers:
                login = c.get("email") or c.get("phone") or f"[no contact — {c.get('shopify_customer_id')}]"
                print(f"  {login}")
        return result

    print(f"Starting migration of {len(customers)} customers...")

    for i, customer in enumerate(customers, 1):
        email = customer["email"]
        phone = customer["phone"]
        login_id = email or phone

        if not login_id:
            # Both parser and fetch_customers filter these out, but guard here
            # as a safety net so a data-quality issue can't cause a crash.
            result["skipped"] += 1
            logging.warning(
                f"Customer ID {customer.get('shopify_customer_id')} has no email or phone — skipped."
            )
            if i % 50 == 0:
                print(f"Still working, migrated {i} customers...")
            continue

        custom_attributes = {
            "shopify_customer_id": customer.get("shopify_customer_id", ""),
            "shopify_total_spent": customer.get("total_spent", ""),
            "shopify_total_orders": customer.get("total_orders", ""),
            "shopify_tags": customer.get("tags", ""),
            "shopify_note": customer.get("note", ""),
        }

        # ── Check for pre-existing Descope user with same email ───────────────
        existing_users = []
        if email:
            try:
                resp = _client().mgmt.user.search_all(emails=[email])
                existing_users = resp.get("users", [])
            except AuthException as e:
                logging.warning(
                    f"Could not search for existing user with email '{email}': {e.error_message}. "
                    "Skipping customer to avoid creating a duplicate."
                )
                result["failed"].append(f"{login_id} — search failed: {e.error_message}")
                continue

        if existing_users:
            # ── Merge into existing user ──────────────────────────────────────
            existing = existing_users[0]
            _existing_login_ids = existing.get("loginIds") or []
            if not _existing_login_ids:
                logging.error(f"Existing user matched for '{login_id}' has no loginIds — skipping merge.")
                result["failed"].append(f"{login_id} — matched user has no loginIds")
                if i % 50 == 0:
                    print(f"Still working, migrated {i} customers...")
                continue
            existing_login_id = _existing_login_ids[0]
            existing_attrs = existing.get("customAttributes") or {}

            merged_attrs = {**existing_attrs, **custom_attributes}

            # Add phone as an additional login ID if Shopify has one and
            # Descope doesn't already have it on this user.
            existing_login_ids_set = set(_existing_login_ids)
            new_login_ids = [phone] if phone and phone not in existing_login_ids_set else []

            patch_kwargs: dict = {"login_id": existing_login_id, "custom_attributes": merged_attrs}
            if new_login_ids:
                patch_kwargs["additional_login_ids"] = new_login_ids

            try:
                _client().mgmt.user.patch(**patch_kwargs)
                result["merged"] += 1
                if new_login_ids:
                    logging.info(
                        f"Merged customer attributes into existing user '{existing_login_id}' "
                        f"and added phone login ID."
                    )
                else:
                    logging.info(f"Merged customer attributes into existing user '{existing_login_id}'.")
            except AuthException as e:
                result["failed"].append(f"{login_id} — {e.error_message}")
                logging.error(f"Failed to merge customer '{login_id}': {e.error_message}")

        else:
            # ── Create new customer ───────────────────────────────────────────
            additional_login_ids = [phone] if email and phone else []

            try:
                _client().mgmt.user.create(
                    login_id=login_id,
                    email=email,
                    phone=phone,
                    given_name=customer.get("given_name"),
                    family_name=customer.get("family_name"),
                    custom_attributes=custom_attributes,
                    verified_email=bool(email),
                    verified_phone=False,
                    additional_login_ids=additional_login_ids,
                )
                _client().mgmt.user.activate(login_id=login_id)
                result["created"] += 1
                logging.info(f"Customer '{login_id}' created.")
            except AuthException as e:
                if _parse_error_code(e) == _ERR_USER_EXISTS:
                    # Phone-only duplicate or race condition — search by phone
                    # and apply the same merge logic used in the email path.
                    logging.info(f"Customer '{login_id}' already existed — attempting merge.")
                    phone_matches = []
                    if not phone:
                        logging.error(
                            f"Customer '{login_id}' already exists in Descope but "
                            "cannot be located for merge (email search found nothing "
                            "and there is no phone to search by). "
                            "Shopify attributes were NOT applied."
                        )
                        result["failed"].append(
                            f"{login_id} — already exists, no phone for merge lookup"
                        )
                    else:
                        try:
                            lookup = _client().mgmt.user.search_all(phones=[phone])
                            phone_matches = lookup.get("users", [])
                        except AuthException as se:
                            logging.warning(
                                f"Could not look up phone-duplicate '{login_id}': {se.error_message}"
                            )

                    if phone_matches:
                        existing = phone_matches[0]
                        _phone_login_ids = existing.get("loginIds") or []
                        if not _phone_login_ids:
                            logging.error(
                                f"Phone-duplicate user for '{login_id}' has no loginIds — skipping merge."
                            )
                            result["failed"].append(f"{login_id} — phone-duplicate has no loginIds")
                        else:
                            existing_login_id = _phone_login_ids[0]
                            existing_attrs = existing.get("customAttributes") or {}
                            merged_attrs = {**existing_attrs, **custom_attributes}

                            # Add email as an additional login ID if Shopify has one
                            # and Descope doesn't already have it on this user.
                            existing_phone_login_ids_set = set(_phone_login_ids)
                            new_phone_login_ids = (
                                [email] if email and email not in existing_phone_login_ids_set else []
                            )

                            phone_patch_kwargs: dict = {
                                "login_id": existing_login_id,
                                "custom_attributes": merged_attrs,
                            }
                            if new_phone_login_ids:
                                phone_patch_kwargs["additional_login_ids"] = new_phone_login_ids

                            try:
                                _client().mgmt.user.patch(**phone_patch_kwargs)
                                result["merged"] += 1
                                if new_phone_login_ids:
                                    logging.info(
                                        f"Merged customer attributes into phone-duplicate '{existing_login_id}' "
                                        f"and added email login ID."
                                    )
                                else:
                                    logging.info(
                                        f"Merged customer attributes into phone-duplicate '{existing_login_id}'."
                                    )
                            except AuthException as me:
                                result["failed"].append(f"{login_id} — merge failed: {me.error_message}")
                                logging.error(
                                    f"Failed to merge phone-duplicate customer '{login_id}': {me.error_message}"
                                )
                    elif phone_matches == [] and phone:
                        # Lookup returned nothing — count as merged to avoid double-counting.
                        result["merged"] += 1
                        logging.warning(
                            f"Customer '{login_id}' already existed but lookup returned no match — "
                            "counted as merged without attribute update."
                        )
                else:
                    result["failed"].append(f"{login_id} — {e.error_message}")
                    logging.error(f"Failed to create customer '{login_id}': {e.error_message}")

        if i % 50 == 0:
            print(f"Still working, migrated {i} customers...")

    return result
