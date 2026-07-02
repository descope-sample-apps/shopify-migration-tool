"""
migration_utils.py

All Descope SDK interactions for the Shopify migration.

Migration order (enforced by main.py):
  1. Roles & permissions  — must exist before users reference them
  2. Staff users          — created with roles
  3. Customers            — created/merged after staff so overlap is handled cleanly

Custom attributes auto-created in Descope before migration begins:
  - shopify_source             (String)   "staff" | "customer"
  - shopify_user_type          (String)   staff only — value passed through from Shopify CSV
                                          (e.g. "Admin", "Point of sale")
  - shpfy_needs_contact        (Boolean)  True for placeholder staff and customer accounts
  - shopify_customer_id        (String)   customers only
  - shopify_total_spent        (String)   customers only
  - shopify_total_orders       (String)   customers only
  - shopify_tags               (String)   customers only

Base tagging roles (always created before migration, independent of --roles):
  - "Staff"    — assigned to every migrated staff user
  - "Customer" — assigned to every migrated customer
  These carry no permissions and exist purely so you can filter users by type
  in the Descope console.

Tenant vs project-level RBAC:
  Roles and permissions are always CREATED at project level (no tenant-scoped
  variant exists in the API). Role ASSIGNMENT differs:
  - No --tenant: roles assigned at project level via add_roles / user_tenants
  - With --tenant: roles assigned within the specified tenant via
    add_tenant_roles / AssociatedTenant, so they apply only in that tenant's
    context.

Accounts with no email and no phone:
  Shopify allows creating staff and customer accounts with only a name. Since
  Descope requires a login ID, these accounts are created with a placeholder login
  ID of the form shopify-{staff|customer}-{id}@noreply.invalid, immediately
  deactivated, and flagged with shpfy_needs_contact=True. Staff roles are
  still assigned. Placeholder accounts are appended to needs_contact_info.csv
  in the working directory so that an operator can update their contact details
  in Descope and reactivate them. The file is opened in append mode so partial
  re-runs (staff only, then customers only) accumulate entries.

Collaborators:
  Shopify staff exports include collaborator accounts (external Shopify Partners).
  These are filtered out before migration and counted in skipped_collaborators.
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
from descope import AuthException, DescopeClient, AssociatedTenant

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
    "shopify_source": "String",
    "shopify_user_type": "String",
    "shpfy_needs_contact": "Boolean",
    "shopify_customer_id": "String",
    "shopify_total_spent": "String",
    "shopify_total_orders": "String",
    "shopify_tags": "String",
}

_PLACEHOLDER_DOMAIN = "noreply.invalid"  # IANA-reserved TLD; can never be a real address


def _placeholder_login_id(prefix: str, shopify_id: str) -> str:
    """
    Generate a placeholder login ID for a user with no email or phone.

    Format: shopify-{prefix}-{shopify_id}@noreply.invalid
    Examples:
      _placeholder_login_id("staff", "266163274")
        → shopify-staff-266163274@noreply.invalid
      _placeholder_login_id("customer", "7452021653713")
        → shopify-customer-7452021653713@noreply.invalid
    """
    return f"shopify-{prefix}-{shopify_id}@{_PLACEHOLDER_DOMAIN}"

_ATTR_TYPE_MAP = {"String": 1, "Number": 2, "Boolean": 3}

# Descope error codes
_ERR_ALREADY_EXISTS = "E024104"  # permission already exists
_ERR_ROLE_EXISTS = "E024209"     # role already exists (different code from permissions)
_ERR_USER_EXISTS = "E011001"     # user already exists (login ID taken)


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


def _assign_roles(login_id: str, role_names: list[str], tenant_id: str | None) -> None:
    """
    Assign roles to an existing user at project or tenant level.

    Used for the skipped_existing path (user already created) and the customer
    merge path. For new user creation, prefer passing user_tenants / role_names
    directly to mgmt.user.create so the assignment is folded into one API call.
    """
    if tenant_id:
        _client().mgmt.user.add_tenant_roles(
            login_id=login_id, tenant_id=tenant_id, role_names=role_names
        )
    else:
        _client().mgmt.user.add_roles(login_id=login_id, role_names=role_names)


# ── Phase 0: Custom attributes ────────────────────────────────────────────────

def ensure_custom_attributes() -> None:
    """
    Auto-create all required custom attributes in Descope, one at a time.
    Safe to call repeatedly — existing attributes are silently skipped.

    Each attribute is posted individually so a single failure (e.g. name too
    long, already exists) does not silently swallow the remaining attributes.

    Note: the Descope Python SDK does not expose custom attribute management,
    so this function calls the REST API directly.
    """
    _ERR_ATTR_EXISTS = "E016002"  # attribute name already taken

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


# ── Pre-migration: Base tagging roles ─────────────────────────────────────────

def ensure_base_roles(migrate_staff: bool, migrate_customers: bool) -> None:
    """
    Create the base tagging roles used to identify migrated users in Descope.

    - "Staff"    — assigned to every migrated staff user
    - "Customer" — assigned to every migrated customer

    These roles carry no permissions; they exist purely as identity tags so
    you can filter users by type in the Descope console. They are created here
    (not inside process_roles) so they are always present regardless of whether
    --roles is provided or which populations are being migrated.

    Already-existing roles are silently skipped.
    """
    base_roles = []
    if migrate_staff:
        base_roles.append(("Staff", "Tagging role for all migrated Shopify staff users"))
    if migrate_customers:
        base_roles.append(("Customer", "Tagging role for all migrated Shopify customers"))

    for name, description in base_roles:
        try:
            _client().mgmt.role.create(name=name, description=description, permission_names=[])
            logging.info(f"Base role '{name}' created.")
        except AuthException as e:
            if _parse_error_code(e) == _ERR_ROLE_EXISTS:
                logging.info(f"Base role '{name}' already exists — skipping.")
            else:
                logging.error(f"Failed to create base role '{name}': {e.error_message}")


# ── Phase 1: Roles & permissions ──────────────────────────────────────────────

def process_roles(
    roles: list[dict], dry_run: bool, verbose: bool
) -> dict:
    """
    Create Shopify roles and their permissions in Descope.

    The "Staff" and "Customer" tagging roles are created separately by
    ensure_base_roles() before this runs, so they are always present
    regardless of whether --roles is provided.

    Args:
        roles: Normalized role list from shopify_parser.parse_roles()

    Returns a result dict with counts and failure lists.
    """
    result = {
        "total": len(roles),
        "created": 0,
        "skipped_existing": 0,
        "failed": [],
        "permissions_created": 0,
        "permissions_skipped": 0,
        "permissions_failed": [],
    }

    if dry_run:
        print(f"Would migrate {len(roles)} Shopify roles to Descope")
        if verbose:
            for role in roles:
                print(f"  Role: {role['name']} ({len(role['permissions'])} permissions)")
        return result

    print(f"Starting migration of {len(roles)} roles...")

    for role in roles:
        perm_names = []

        # Create permissions first
        for perm in role["permissions"]:
            try:
                _client().mgmt.permission.create(name=perm)
                perm_names.append(perm)
                result["permissions_created"] += 1
            except AuthException as e:
                if _parse_error_code(e) == _ERR_ALREADY_EXISTS:
                    perm_names.append(perm)
                    result["permissions_skipped"] += 1
                    logging.info(f"Permission '{perm}' already exists — skipping.")
                else:
                    result["permissions_failed"].append(
                        f"{perm} (role: {role['name']}) — {e.error_message}"
                    )
                    logging.error(f"Failed to create permission '{perm}': {e.error_message}")

        # Create the role
        try:
            _client().mgmt.role.create(
                name=role["name"],
                description=f"Shopify role: {role['name']} [{role['category']}]",
                permission_names=perm_names,
            )
            result["created"] += 1
            logging.info(f"Role '{role['name']}' created.")
        except AuthException as e:
            if _parse_error_code(e) == _ERR_ROLE_EXISTS:
                result["skipped_existing"] += 1
                logging.info(f"Role '{role['name']}' already exists — updating permissions.")
                # Update the role's permission set so re-runs propagate permission changes.
                try:
                    _client().mgmt.role.update(
                        name=role["name"],
                        new_name=role["name"],
                        description=f"Shopify role: {role['name']} [{role['category']}]",
                        permission_names=perm_names,
                    )
                    logging.info(f"Role '{role['name']}' permissions updated.")
                except AuthException as ue:
                    logging.warning(f"Could not update permissions for existing role '{role['name']}': {ue.error_message}")
            else:
                result["failed"].append(f"{role['name']} — {e.error_message}")
                logging.error(f"Failed to create role '{role['name']}': {e.error_message}")

    return result


# ── Phase 2: Staff users ──────────────────────────────────────────────────────

def process_staff_users(
    staff: list[dict], dry_run: bool, verbose: bool, tenant_id: str | None = None
) -> dict:
    """
    Create staff users in Descope and assign their Shopify roles.

    Staff accounts with no email and no phone are created with a placeholder
    login ID (shopify-staff-{id}@noreply.invalid), immediately deactivated,
    and flagged with shpfy_needs_contact=True. Their roles are still
    assigned. Callers should write the returned 'placeholders' list to a CSV.

    Args:
        staff:     Normalized staff list from shopify_parser.parse_staff_users()
        tenant_id: If provided, roles are assigned within this Descope tenant.
                   If None, roles are assigned at project level.
    """
    result = {
        "total": len(staff),
        "created": 0,
        "skipped_existing": 0,
        "failed": [],
        "roles_failed": [],
        # Each entry: {user_type, shopify_id, given_name, family_name, roles, placeholder_login_id}
        "placeholders": [],
        "skipped_collaborators": 0,
    }

    # Collaborators are external Shopify Partners — skip, don't migrate.
    collaborators = [u for u in staff if u.get("user_type", "").lower() == "collaborator"]
    staff = [u for u in staff if u.get("user_type", "").lower() != "collaborator"]
    result["skipped_collaborators"] = len(collaborators)
    if collaborators:
        logging.warning(
            f"Skipping {len(collaborators)} collaborator account(s) — "
            "collaborators are external Shopify Partners and are not migrated."
        )

    if dry_run:
        no_contact = [u for u in staff if not u.get("email") and not u.get("phone")]
        with_contact = [u for u in staff if u.get("email") or u.get("phone")]
        scope = f"tenant '{tenant_id}'" if tenant_id else "project level"
        print(f"Would migrate {len(with_contact)} staff users to Descope ({scope} roles)")
        if collaborators:
            print(f"Would skip {len(collaborators)} collaborator account(s)")
        if no_contact:
            print(
                f"Would create {len(no_contact)} placeholder staff account(s) "
                f"(no email or phone) and write needs_contact_info.csv"
            )
        if verbose:
            for u in staff:
                login = u.get("email") or u.get("phone") or f"[placeholder: {_placeholder_login_id('staff', u['shopify_user_id'])}]"
                print(f"  {login} — roles: {', '.join(u['roles']) or 'none'}")
        return result

    scope = f"tenant '{tenant_id}'" if tenant_id else "project level"
    print(f"Starting migration of {len(staff)} staff users ({scope} roles)...")

    for user in staff:
        email = user.get("email")
        phone = user.get("phone")
        # "Active" → active; "Pending" (invite not yet accepted) and "Inactive" → deactivated
        active = user["status"].lower() == "active"
        is_placeholder = not email and not phone

        if is_placeholder:
            login_id = _placeholder_login_id("staff", user["shopify_user_id"])
            custom_attributes = {
                "shopify_source": "staff",
                "shopify_user_type": user.get("user_type", ""),
                "shpfy_needs_contact": True,
            }
        else:
            login_id = email or phone
            custom_attributes = {
                "shopify_source": "staff",
                "shopify_user_type": user.get("user_type", ""),
                "shpfy_needs_contact": False,
            }

        # Always include "Staff" tagging role + any Shopify roles from CSV.
        all_roles = ["Staff"] + user["roles"]

        # Track whether the user already existed so we know whether to touch activation.
        already_existed = False

        # In tenant mode, fold role assignment into the create call via user_tenants
        # to save a round-trip. In project mode, pass role_names directly.
        try:
            if tenant_id:
                _client().mgmt.user.create(
                    login_id=login_id,
                    email=email if not is_placeholder else None,
                    phone=phone,
                    given_name=user.get("given_name"),
                    family_name=user.get("family_name"),
                    custom_attributes=custom_attributes,
                    verified_email=bool(email and not is_placeholder),
                    user_tenants=[AssociatedTenant(tenant_id, role_names=all_roles)],
                )
            else:
                _client().mgmt.user.create(
                    login_id=login_id,
                    email=email if not is_placeholder else None,
                    phone=phone,
                    given_name=user.get("given_name"),
                    family_name=user.get("family_name"),
                    custom_attributes=custom_attributes,
                    verified_email=bool(email and not is_placeholder),
                    role_names=all_roles,
                )
            result["created"] += 1
            if is_placeholder:
                result["placeholders"].append({
                    "user_type": "staff",
                    "shopify_id": user["shopify_user_id"],
                    "given_name": user.get("given_name", ""),
                    "family_name": user.get("family_name", ""),
                    "roles": ", ".join(user["roles"]),
                    "placeholder_login_id": login_id,
                })
                logging.warning(
                    f"Staff user '{user.get('given_name', '')} {user.get('family_name', '')}' "
                    f"(ID {user['shopify_user_id']}) has no contact info — "
                    f"created with placeholder login ID '{login_id}'."
                )
            else:
                logging.info(f"Staff user '{login_id}' created.")
            if verbose:
                print(f"  {login_id} — assigned roles: {', '.join(all_roles)}")

        except AuthException as e:
            if _parse_error_code(e) == _ERR_USER_EXISTS:
                already_existed = True
                result["skipped_existing"] += 1
                logging.info(f"Staff user '{login_id}' already exists — updating roles.")
                # User exists: still assign roles so re-runs are idempotent.
                try:
                    _assign_roles(login_id, all_roles, tenant_id)
                    if verbose:
                        print(f"  {login_id} — roles updated: {', '.join(all_roles)}")
                except AuthException as re:
                    result["roles_failed"].append(f"{login_id} roles {all_roles} — {re.error_message}")
                    logging.error(f"Failed to assign roles to existing user '{login_id}': {re.error_message}")
            else:
                result["failed"].append(f"{login_id} — {e.error_message}")
                logging.error(f"Failed to create staff user '{login_id}': {e.error_message}")
                continue  # Don't attempt activate/deactivate if creation failed

        # Placeholder accounts are always deactivated regardless of Shopify status.
        # Newly created normal accounts honour the Shopify active/inactive status.
        # Existing users (re-run): skip activation to avoid overwriting manual changes
        # made in the Descope console since the previous run.
        if not already_existed:
            try:
                if is_placeholder or not active:
                    _client().mgmt.user.deactivate(login_id=login_id)
                else:
                    _client().mgmt.user.activate(login_id=login_id)
            except AuthException as e:
                logging.warning(f"Could not set active status for '{login_id}': {e.error_message}")

    return result


# ── Phase 3: Customers ────────────────────────────────────────────────────────

def process_customers(
    customers: list[dict], dry_run: bool, verbose: bool, tenant_id: str | None = None
) -> dict:
    """
    Create customer users in Descope (or merge into an existing record if a
    staff member shares the same email or phone).

    Merge behaviour:
      - If a customer's email matches an existing Descope user (e.g. a staff
        member), the customer's Shopify attributes (shopify_customer_id,
        shopify_total_spent, shopify_total_orders, shopify_tags) are patched
        onto the existing record and the "Customer" tagging role is added.
        shopify_source stays "staff" — staff takes precedence.
      - If create() fails with a user-already-exists error and no email match
        was found, the code searches by phone to find the duplicate and applies
        the same patch + role logic.
      - Activation state is intentionally left untouched for merged users to
        avoid overwriting manual changes made in the Descope console.

    Args:
        customers: Normalized customer list from shopify_parser or shopify_client
        tenant_id: If provided, roles are assigned within this Descope tenant.
                   If None, roles are assigned at project level.
    """
    result = {
        "total": len(customers),
        "created": 0,
        "merged": 0,       # existing user updated with customer attributes
        "failed": [],
        # Each entry: {user_type, shopify_id, given_name, family_name, roles, placeholder_login_id}
        "placeholders": [],
    }

    if dry_run:
        no_contact = [c for c in customers if not c.get("email") and not c.get("phone")]
        with_contact = [c for c in customers if c.get("email") or c.get("phone")]
        scope = f"tenant '{tenant_id}'" if tenant_id else "project level"
        print(f"Would migrate {len(with_contact)} customers to Descope ({scope} roles)")
        if no_contact:
            print(
                f"Would create {len(no_contact)} placeholder customer account(s) "
                f"(no email or phone) and append to needs_contact_info.csv"
            )
        if verbose:
            for c in customers:
                login = c.get("email") or c.get("phone") or f"[placeholder: {_placeholder_login_id('customer', c['shopify_customer_id'])}]"
                print(f"  {login}")
        return result

    scope = f"tenant '{tenant_id}'" if tenant_id else "project level"
    print(f"Starting migration of {len(customers)} customers ({scope} roles)...")

    for i, customer in enumerate(customers, 1):
        email = customer["email"]
        phone = customer["phone"]
        is_placeholder = not email and not phone
        login_id = email or phone or _placeholder_login_id("customer", customer["shopify_customer_id"])

        custom_attributes = {
            "shopify_source": "customer",
            "shpfy_needs_contact": is_placeholder,
            "shopify_customer_id": customer.get("shopify_customer_id", ""),
            "shopify_total_spent": customer.get("total_spent", ""),
            "shopify_total_orders": customer.get("total_orders", ""),
            "shopify_tags": customer.get("tags", ""),
        }

        # Placeholder customers: no email/phone to search by or log in with.
        if is_placeholder:
            created_now = False
            try:
                if tenant_id:
                    _client().mgmt.user.create(
                        login_id=login_id,
                        given_name=customer.get("given_name"),
                        family_name=customer.get("family_name"),
                        custom_attributes=custom_attributes,
                        verified_email=False,
                        user_tenants=[AssociatedTenant(tenant_id, role_names=["Customer"])],
                    )
                else:
                    _client().mgmt.user.create(
                        login_id=login_id,
                        given_name=customer.get("given_name"),
                        family_name=customer.get("family_name"),
                        custom_attributes=custom_attributes,
                        verified_email=False,
                        role_names=["Customer"],
                    )
                created_now = True
                result["placeholders"].append({
                    "user_type": "customer",
                    "shopify_id": customer.get("shopify_customer_id", ""),
                    "given_name": customer.get("given_name", ""),
                    "family_name": customer.get("family_name", ""),
                    "roles": "Customer",
                    "placeholder_login_id": login_id,
                })
                result["created"] += 1
                logging.warning(
                    f"Customer ID {customer.get('shopify_customer_id')} has no contact info — "
                    f"created with placeholder login ID '{login_id}'."
                )
            except AuthException as e:
                if _parse_error_code(e) == _ERR_USER_EXISTS:
                    result["merged"] += 1
                    logging.info(f"Placeholder customer '{login_id}' already existed — counted as merged.")
                else:
                    result["failed"].append(f"{login_id} — {e.error_message}")
                    logging.error(f"Failed to create placeholder customer '{login_id}': {e.error_message}")

            # Deactivate outside the create() try so a deactivation failure
            # doesn't cause the user to be miscounted as failed.
            if created_now:
                try:
                    _client().mgmt.user.deactivate(login_id=login_id)
                except AuthException as e:
                    logging.warning(f"Could not deactivate placeholder customer '{login_id}': {e.error_message}")

            if i % 50 == 0:
                print(f"Still working, migrated {i} customers...")
            continue

        # Check if user already exists (overlap with staff)
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
            # Merge: update custom attributes on existing user, add Customer role
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

            # Staff takes precedence for shopify_source; add customer attrs
            merged_attrs = {**existing_attrs}
            for key in [
                "shopify_customer_id",
                "shopify_total_spent",
                "shopify_total_orders",
                "shopify_tags",
            ]:
                merged_attrs[key] = custom_attributes[key]

            try:
                # patch() only writes the fields we pass — existing email, phone,
                # verified_email, etc. are left untouched automatically.
                # Activation state is also left untouched — we don't want to
                # override manual changes made in the Descope console.
                _client().mgmt.user.patch(
                    login_id=existing_login_id,
                    custom_attributes=merged_attrs,
                )
                _assign_roles(existing_login_id, ["Customer"], tenant_id)
                result["merged"] += 1
                logging.info(f"Merged customer attributes into existing user '{existing_login_id}'.")
            except AuthException as e:
                result["failed"].append(f"{login_id} — {e.error_message}")
                logging.error(f"Failed to merge customer '{login_id}': {e.error_message}")

        else:
            # New user with contact info
            additional_login_ids = [phone] if email and phone else []

            try:
                if tenant_id:
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
                        user_tenants=[AssociatedTenant(tenant_id, role_names=["Customer"])],
                    )
                else:
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
                        role_names=["Customer"],
                    )
                result["created"] += 1
                logging.info(f"Customer '{login_id}' created.")
            except AuthException as e:
                if _parse_error_code(e) == _ERR_USER_EXISTS:
                    # Phone-only duplicate or race condition — look up the existing
                    # user and apply the same merge logic used in the email path.
                    logging.info(
                        f"Customer '{login_id}' already existed — attempting merge."
                    )
                    if not phone:
                        # Email-only customer: no phone to search by, so the
                        # conflicting record can't be identified for merge.
                        phone_matches = []
                        logging.error(
                            f"Customer '{login_id}' already exists in Descope but "
                            "cannot be located for merge (email search found nothing "
                            "and there is no phone to search by). "
                            "Shopify attributes and Customer role were NOT applied."
                        )
                        result["failed"].append(
                            f"{login_id} — already exists, no phone for merge lookup"
                        )
                    else:
                        try:
                            lookup = _client().mgmt.user.search_all(phones=[phone])
                            phone_matches = lookup.get("users", [])
                        except AuthException as se:
                            phone_matches = []
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
                            merged_attrs = {**existing_attrs}
                            for key in [
                                "shopify_customer_id",
                                "shopify_total_spent",
                                "shopify_total_orders",
                                "shopify_tags",
                            ]:
                                merged_attrs[key] = custom_attributes[key]
                            try:
                                # patch() only writes the fields we pass — existing email,
                                # phone, verified_email, etc. are left untouched automatically.
                                _client().mgmt.user.patch(
                                    login_id=existing_login_id,
                                    custom_attributes=merged_attrs,
                                )
                                _assign_roles(existing_login_id, ["Customer"], tenant_id)
                                result["merged"] += 1
                                logging.info(
                                    f"Merged customer attributes into phone-duplicate '{existing_login_id}'."
                                )
                            except AuthException as me:
                                result["failed"].append(f"{login_id} — merge failed: {me.error_message}")
                                logging.error(
                                    f"Failed to merge phone-duplicate customer '{login_id}': {me.error_message}"
                                )
                    else:
                        # Couldn't look up the existing user; count as merged to avoid double-counting.
                        result["merged"] += 1
                        logging.warning(
                            f"Customer '{login_id}' already existed but lookup returned no match — "
                            "counted as merged without attribute update."
                        )
                else:
                    result["failed"].append(f"{login_id} — {e.error_message}")
                    logging.error(f"Failed to create customer '{login_id}': {e.error_message}")
            else:
                # Activate outside the create() try so an activation failure
                # doesn't cause the user to be miscounted as failed.
                try:
                    _client().mgmt.user.activate(login_id=login_id)
                except AuthException as e:
                    logging.warning(f"Could not activate customer '{login_id}': {e.error_message}")

        if i % 50 == 0:
            print(f"Still working, migrated {i} customers...")

    return result
