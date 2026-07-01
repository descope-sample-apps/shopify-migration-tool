"""
shopify_client.py

Fetches customer data from the Shopify GraphQL Admin API and normalizes it
to the same structure that shopify_parser.parse_customers() returns, so
migration_utils.py is source-agnostic.

Note: Staff users and roles are NOT available via this client — the GraphQL
staffMembers query returns no role/permission data, and is only accessible on
Shopify Plus/Advanced stores with a manually approved read_users scope.
When migrating staff or roles, those must come from CSV exports — not this client.

Authentication:
  Set SHOPIFY_SHOP_URL and SHOPIFY_ACCESS_TOKEN in your .env file.
  The access token must have the read_customers scope.
"""

import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "").strip().rstrip("/")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()

# Use the stable 2026-07 API version
_API_VERSION = "2026-07"
_PAGE_SIZE = 250  # maximum allowed by Shopify

_CUSTOMERS_QUERY = """
query fetchCustomers($first: Int!, $after: String) {
  customers(first: $first, after: $after) {
    edges {
      cursor
      node {
        id
        firstName
        lastName
        defaultEmailAddress {
          emailAddress
        }
        defaultPhoneNumber {
          phoneNumber
        }
        numberOfOrders
        amountSpent {
          amount
          currencyCode
        }
        tags
        note
        state
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def _graphql_request(query: str, variables: dict, max_retries: int = 4) -> dict:
    """
    Execute a GraphQL request against the Shopify Admin API with retry on
    rate limiting (429) and transient errors.
    """
    if not SHOPIFY_SHOP_URL or not SHOPIFY_ACCESS_TOKEN:
        logging.error(
            "SHOPIFY_SHOP_URL and SHOPIFY_ACCESS_TOKEN must be set in .env when using --from-api."
        )
        sys.exit(1)

    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables}

    retries = 0
    while retries <= max_retries:
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)

            if response.status_code == 429:
                wait = 5 ** (retries + 1)
                logging.info(f"Rate limited by Shopify. Retrying in {wait}s...")
                time.sleep(wait)
                retries += 1
                continue

            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logging.error(f"GraphQL errors: {data['errors']}")
                return {}

            return data.get("data", {})

        except requests.exceptions.Timeout:
            wait = 5 ** (retries + 1)
            logging.warning(f"Request timed out. Retrying in {wait}s... ({retries + 1}/{max_retries})")
            time.sleep(wait)
            retries += 1

        except requests.exceptions.RequestException as e:
            logging.error(f"Request failed: {e}")
            return {}

    logging.error("Max retries reached fetching from Shopify API.")
    return {}


def _normalize_customer(node: dict) -> dict:
    """
    Normalize a GraphQL Customer node to the same structure that
    shopify_parser.parse_customers() returns.
    """
    # Strip the GID prefix: "gid://shopify/Customer/12345" -> "12345"
    raw_id = node.get("id", "")
    shopify_id = raw_id.split("/")[-1] if "/" in raw_id else raw_id

    email_obj = node.get("defaultEmailAddress") or {}
    phone_obj = node.get("defaultPhoneNumber") or {}
    amount_obj = node.get("amountSpent") or {}

    tags = node.get("tags") or []
    tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)

    return {
        "shopify_customer_id": shopify_id,
        "email": email_obj.get("emailAddress") or None,
        "phone": phone_obj.get("phoneNumber") or None,
        "given_name": node.get("firstName") or None,
        "family_name": node.get("lastName") or None,
        "total_spent": str(amount_obj.get("amount", "0.00")),
        "total_orders": str(node.get("numberOfOrders", "0")),
        "tags": tags_str,
        "note": node.get("note") or "",
    }


def fetch_customers() -> list[dict]:
    """
    Fetch all customers from the Shopify GraphQL API using cursor-based
    pagination, and return them normalized to the same structure as
    shopify_parser.parse_customers().
    """
    customers = []
    cursor = None
    page = 1

    while True:
        variables = {"first": _PAGE_SIZE, "after": cursor}
        data = _graphql_request(_CUSTOMERS_QUERY, variables)

        customers_data = data.get("customers", {})
        edges = customers_data.get("edges", [])
        page_info = customers_data.get("pageInfo", {})

        if not edges:
            break

        for edge in edges:
            node = edge.get("node", {})
            normalized = _normalize_customer(node)

            # Skip records with no usable login ID
            if not normalized["email"] and not normalized["phone"]:
                logging.warning(
                    f"Skipping customer ID {normalized['shopify_customer_id']} "
                    f"— no email or phone."
                )
                continue

            customers.append(normalized)
            cursor = edge.get("cursor")

        logging.info(f"Fetched page {page} — {len(customers)} customers so far.")
        page += 1

        if not page_info.get("hasNextPage"):
            break

    logging.info(f"Fetched {len(customers)} total customers from Shopify API.")
    return customers
