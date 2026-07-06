"""
shopify_client.py

Fetches customer data from the Shopify GraphQL Admin API and normalizes it
to the same structure that shopify_parser.parse_customers() returns, so
migration_utils.py is source-agnostic.

Authentication (--from-api mode):
  Option 1 — static token:
    Set SHOPIFY_ACCESS_TOKEN in .env directly. The script uses it as-is.

  Option 2 — OAuth flow:
    Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (from your Shopify Dev Dashboard
    app) plus SHOPIFY_SHOP_URL in .env. In your app's configuration, add
    http://localhost:{SHOPIFY_OAUTH_PORT}/callback as an allowed redirect URI
    (default port: 3000). When --from-api is used without a token present, the
    script opens your browser to authorize, catches the redirect, exchanges the
    code for an access token, saves it to .env, and continues.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import logging
import os
import secrets
import sys
import time
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL", "").strip().rstrip("/")
if SHOPIFY_SHOP_URL.startswith(("http://", "https://")):
    print(
        "Error: SHOPIFY_SHOP_URL must not include the scheme. "
        "Use 'my-store.myshopify.com', not 'https://my-store.myshopify.com'."
    )
    sys.exit(1)
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()

_OAUTH_PORT = int(os.getenv("SHOPIFY_OAUTH_PORT", "3000"))
_OAUTH_REDIRECT_URI = f"http://localhost:{_OAUTH_PORT}/callback"
_OAUTH_SCOPES = "read_customers"
_OAUTH_TIMEOUT_SECONDS = 120

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


def _validate_shopify_hmac(params: dict[str, list[str]], secret: str) -> bool:
    """
    Validate the HMAC signature Shopify appends to OAuth redirect URLs.
    Pops 'hmac' from params, sorts the rest, and compares a SHA-256 digest.
    """
    hmac_val = params.pop("hmac", [""])[0]
    message = "&".join(f"{k}={v[0]}" for k, v in sorted(params.items()))
    digest = hmac_lib.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac_lib.compare_digest(digest, hmac_val)


def _save_token_to_env(token: str) -> None:
    """Write or update SHOPIFY_ACCESS_TOKEN in the project root .env file."""
    env_path = str(Path(__file__).parent.parent / ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith("SHOPIFY_ACCESS_TOKEN="):
                lines[i] = f"SHOPIFY_ACCESS_TOKEN={token}\n"
                break
        else:
            lines.append(f"SHOPIFY_ACCESS_TOKEN={token}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
    else:
        with open(env_path, "a") as f:
            f.write(f"SHOPIFY_ACCESS_TOKEN={token}\n")


def authenticate_shopify() -> None:
    """
    Ensure SHOPIFY_ACCESS_TOKEN is available. If it is already set in .env,
    return immediately. Otherwise, run the OAuth 2.0 authorization code flow:

      1. Open the browser to Shopify's authorize URL.
      2. Start a local HTTP server to catch the redirect callback.
      3. Validate the HMAC and state nonce, then exchange the code for a token.
      4. Save the token to .env and update the module-level variable.

    Requires SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, and SHOPIFY_SHOP_URL.
    """
    global SHOPIFY_ACCESS_TOKEN

    if SHOPIFY_ACCESS_TOKEN:
        return

    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        print(
            "Error: no Shopify credentials found for --from-api.\n"
            "Set one of the following in .env:\n"
            "  • SHOPIFY_ACCESS_TOKEN  — a token you already have, or\n"
            "  • SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET  — to obtain one via OAuth.\n"
            "See README.md for setup instructions."
        )
        sys.exit(1)

    if not SHOPIFY_SHOP_URL:
        print("Error: SHOPIFY_SHOP_URL must be set in .env.")
        sys.exit(1)

    state = secrets.token_hex(16)
    result: dict[str, str | None] = {"token": None, "error": None}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self._respond(404, "Not found.")
                return

            params = parse_qs(parsed.query)

            if params.get("state", [""])[0] != state:
                result["error"] = "State mismatch — possible CSRF."
                self._respond(400, "Authentication failed: state mismatch. You can close this tab.")
                return

            if not _validate_shopify_hmac(dict(params), SHOPIFY_CLIENT_SECRET):
                result["error"] = "HMAC validation failed."
                self._respond(400, "Authentication failed: invalid HMAC. You can close this tab.")
                return

            code = params.get("code", [""])[0]
            if not code:
                result["error"] = "No authorization code in callback."
                self._respond(400, "Authentication failed: no code. You can close this tab.")
                return

            try:
                resp = requests.post(
                    f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token",
                    json={
                        "client_id": SHOPIFY_CLIENT_ID,
                        "client_secret": SHOPIFY_CLIENT_SECRET,
                        "code": code,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                token = resp.json().get("access_token", "")
                if token:
                    result["token"] = token
                    self._respond(200, "Authorization successful! You can close this tab and return to the terminal.")
                else:
                    result["error"] = "Token exchange response contained no access_token."
                    self._respond(500, "Authentication failed: no token in response. You can close this tab.")
            except requests.exceptions.RequestException as e:
                result["error"] = f"Token exchange request failed: {e}"
                self._respond(500, "Authentication failed. Check the terminal for details.")

        def _respond(self, status: int, message: str) -> None:
            body = f"<h2>{message}</h2>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # suppress default HTTP server logs

    auth_url = (
        f"https://{SHOPIFY_SHOP_URL}/admin/oauth/authorize?"
        + urlencode({
            "client_id": SHOPIFY_CLIENT_ID,
            "scope": _OAUTH_SCOPES,
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "state": state,
        })
    )

    print(f"Opening browser for Shopify authorization...")
    print(f"Redirect URI (must match your Dev Dashboard app config): {_OAUTH_REDIRECT_URI}")
    print(f"\nIf the browser doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", _OAUTH_PORT), _CallbackHandler)
    server.timeout = _OAUTH_TIMEOUT_SECONDS
    print(f"Waiting for authorization (timeout: {_OAUTH_TIMEOUT_SECONDS}s)...")
    server.handle_request()
    server.server_close()

    if result["error"]:
        print(f"Error: OAuth failed — {result['error']}")
        sys.exit(1)

    if not result["token"]:
        print("Error: timed out waiting for the browser callback. Please try again.")
        sys.exit(1)

    SHOPIFY_ACCESS_TOKEN = result["token"]
    _save_token_to_env(SHOPIFY_ACCESS_TOKEN)
    print("Access token obtained and saved to .env.\n")


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
                wait = min(5 ** (retries + 1), 60)
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
            wait = min(5 ** (retries + 1), 60)
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

    Calls authenticate_shopify() first to ensure a valid access token is present,
    running the OAuth flow if needed.
    """
    authenticate_shopify()

    customers = []
    cursor = None
    page = 1

    while True:
        variables = {"first": _PAGE_SIZE, "after": cursor}
        data = _graphql_request(_CUSTOMERS_QUERY, variables)

        # An empty dict means _graphql_request exhausted retries or hit a
        # GraphQL error. Abort rather than silently returning a truncated list.
        if not data or "customers" not in data:
            logging.error(
                f"Shopify API returned no data on page {page} "
                f"({len(customers)} customers fetched so far). Aborting."
            )
            print(
                f"\nError: Shopify API failed on page {page}. "
                f"Only {len(customers)} customer(s) fetched. Check logs for details."
            )
            sys.exit(1)

        customers_data = data.get("customers", {})
        edges = customers_data.get("edges", [])
        page_info = customers_data.get("pageInfo", {})

        if not edges:
            break

        for edge in edges:
            node = edge.get("node", {})
            normalized = _normalize_customer(node)
            customers.append(normalized)
            cursor = edge.get("cursor")

        logging.info(f"Fetched page {page} — {len(customers)} customers so far.")
        page += 1

        if not page_info.get("hasNextPage"):
            break

    logging.info(f"Fetched {len(customers)} total customers from Shopify API.")
    return customers
