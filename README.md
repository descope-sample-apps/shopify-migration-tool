# Shopify → Descope Migration Tool

A Python utility for migrating your Shopify customers to Descope.

---

## ⚠️ Important Notes

### No passwords
Shopify does not export user passwords. All migrated users will have no credentials in Descope. You'll need to configure a Descope flow (magic link, OTP, SSO, etc.) for users to authenticate for the first time after migration, after which they can set a new password if you wish to allow so.

### Customer CSV export cap (15 MB)
Shopify caps customer CSV exports at **15 MB**. For stores with large customer lists:
- Export multiple CSVs using Shopify's segment or date-range filters, then pass them all to `--customers` — duplicates are automatically deduplicated
- Or use `--from-api` which fetches all customers via the GraphQL API with automatic pagination (recommended for large stores)

### Accounts with no contact info
Shopify allows creating customer accounts with only a first and last name — no email or phone. Since Descope requires a login ID, these accounts are handled as follows:
- A placeholder login ID is assigned: `shopify-customer-{id}@noreply.invalid`
- The account is created in Descope but **immediately deactivated**
- The `shpfy_needs_contact` custom attribute is set to `true`
- The account is appended to `needs_contact_info.csv` in your working directory (the file accumulates across partial re-runs)

After migration, search for `shpfy_needs_contact=true` in the Descope console, update each account with a real email or phone number, and reactivate them.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/descope/descope-shopify-migration.git
cd descope-shopify-migration
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
# Descope credentials (always required)
# Project ID: https://app.descope.com/settings/project
DESCOPE_PROJECT_ID=

# Management Key: https://app.descope.com/settings/company/managementkeys
DESCOPE_MANAGEMENT_KEY=

# Shopify credentials (only required when using --from-api)
# Your store URL, e.g. my-store.myshopify.com (no https://)
SHOPIFY_SHOP_URL=

# --- Option 1: static access token ---
# If you already have a Shopify Admin API access token, set it here directly.
# The script will use it as-is and skip the OAuth flow.
SHOPIFY_ACCESS_TOKEN=

# --- Option 2: OAuth flow ---
# If you don't have a token, the script can obtain one automatically via OAuth.
# Create an app in the Shopify Dev Dashboard (https://shopify.dev/docs/apps/build/dev-dashboard),
# add http://localhost:3000/callback as an allowed redirect URI, and paste the
# Client ID and Client secret below. Leave SHOPIFY_ACCESS_TOKEN blank.
# After the first successful run the token will be saved to this file automatically.
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=

# Optional: change the local port used for the OAuth callback (default: 3000).
# If you change this, update the redirect URI in your Shopify app accordingly.
# SHOPIFY_OAUTH_PORT=3000
```

**Getting Descope credentials:**
- Project ID: [app.descope.com/settings/project](https://app.descope.com/settings/project)
- Management Key: [app.descope.com/settings/company/managementkeys](https://app.descope.com/settings/company/managementkeys)

**Getting Shopify credentials (--from-api mode only):**

`SHOPIFY_SHOP_URL` is your store's `.myshopify.com` domain — no `https://` prefix.

For the access token there are two options. **Option 1 (static token):** if you already have a Shopify Admin API token, set `SHOPIFY_ACCESS_TOKEN` directly and leave the OAuth fields blank. **Option 2 (OAuth flow, recommended):** create an app in the [Shopify Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard) with the `read_customers` scope, add `http://localhost:3000/callback` as an allowed redirect URI and add the app to your store, paste the Client ID and Client secret into `.env`, and leave `SHOPIFY_ACCESS_TOKEN` blank. Run the script with `--from-api` and it will open your browser, complete the flow, and save the token automatically.

### 5. Export your Shopify data

From your Shopify Admin: **Customers → Export → Plain CSV file**

Shopify caps the export at 15 MB. For large stores, export in multiple parts using segment or date filters, then pass all files to `--customers`. Alternatively, use `--from-api` to fetch all customers directly via the GraphQL API with automatic pagination.

---

## Running the Migration

### Dry run (recommended first)

Preview what will be migrated without making any changes:

```bash
# From CSV
python3 src/main.py --customers shopify-exports/customers_export.csv --dry-run

# From API
python3 src/main.py --from-api --dry-run
```

### Live migration

```bash
# Single CSV
python3 src/main.py --customers shopify-exports/customers_export.csv

# Multiple CSVs (15 MB export cap workaround)
python3 src/main.py --customers shopify-exports/customers_1.csv \
                                shopify-exports/customers_2.csv

# From Shopify GraphQL API
python3 src/main.py --from-api
```

Add `--verbose` / `-v` to print each customer as it is processed.

### Example output

```
Fetching customers from Shopify GraphQL API...

Loaded: 523 customers.

Ensuring custom attributes exist in Descope...
Starting migration of 523 customers...
Still working, migrated 50 customers...
...

============================================================
MIGRATION SUMMARY
============================================================

── Customers ────────────────────────────────────────────
  Total processed       : 523
  Created               : 521
  Merged into existing  : 2
  Placeholder accounts  : 0
  Failed                : 0

Migration complete. Full log written to logs/
============================================================
```

---

## What Gets Migrated

### Customers
Each customer is created as a Descope user with:
- Email as primary login ID; phone as additional login ID (if present)
- Active status (accounts with no contact info are created deactivated with a placeholder login ID)
- Custom attributes from Shopify (see table below)

### Custom attributes
The following custom attributes are automatically created in your Descope project before migration begins:

| Attribute | Type | Description |
|---|---|---|
| `shpfy_needs_contact` | Boolean | `true` if the account has a placeholder login ID (no email or phone) |
| `shopify_customer_id` | String | Shopify customer ID |
| `shopify_total_spent` | String | Lifetime spend value |
| `shopify_total_orders` | String | Total order count |
| `shopify_tags` | String | Comma-separated Shopify tags |
| `shopify_note` | String | Internal Shopify note |

### Existing Descope users
If a customer's email already exists in Descope (e.g. a pre-existing user), the migration merges in the Shopify attributes without touching the existing account's activation state or other data.

---

## Testing

```bash
python3 -m unittest tests.test_migration
```

---

## Logs

A timestamped log file is written to `logs/migration_log_<timestamp>.log` on each run. Failed users are listed in both the log and the terminal summary.

---

## License

MIT
