# Shopify → Descope Migration Tool

A Python utility for migrating your Shopify customers and staff users to Descope.

Migrates:
- **Staff users** with their **roles** and permissions (from CSV exports)
- **Customers** with their Shopify metadata (from CSV exports or the Shopify GraphQL API)

---

## ⚠️ Important Notes

### No passwords
Shopify does not export user passwords. All migrated users will have no credentials in Descope. You'll need to configure a Descope flow (magic link, OTP, SSO, etc.) for users to authenticate for the first time after migration, after which they can set a new password if you wish.

### Customer CSV export cap (15 MB)
Shopify caps customer CSV exports at **15 MB**. For stores with large customer lists:
- Export multiple CSVs using Shopify's segment or date-range filters, then pass them all to `--customers` — duplicates are automatically deduplicated
- Or use `--from-api` which fetches all customers via the GraphQL API with automatic pagination (recommended for large stores)

### Accounts with no contact info
Shopify allows creating staff accounts with only a first and last name — no email or phone. Customers can also be created without contact info. Since Descope requires a login ID, these accounts are handled as follows:
- A placeholder login ID is assigned: `shopify-staff-{id}@noreply.invalid` (staff) or `shopify-customer-{id}@noreply.invalid` (customers)
- The account is created in Descope with roles intact but **immediately deactivated**
- The `shpfy_needs_contact` custom attribute is set to `true`
- The account is appended to `needs_contact_info.csv` in your working directory, with a `user_type` column indicating `"staff"` or `"customer"` (the file accumulates across partial re-runs)

After migration, search for `shpfy_needs_contact=true` in the Descope console, update each account with a real email or phone number, and reactivate them.

### Collaborators
Shopify staff exports include **collaborator** accounts — external Shopify Partners who have been granted store access. These are not your employees and are not migrated. Collaborators are counted in the summary and logged as skipped.

### Project-level vs tenant-level RBAC
By default, roles are assigned at **project level** — they apply globally across all tenants in your Descope project. If your project uses tenants, pass `--tenant <TENANT_ID>` to assign roles within that tenant's context instead.

Note: roles and permissions are always *created* at project level regardless of this flag (the Descope SDK has no tenant-scoped role creation). Only the *assignment* to users differs.

```bash
# Assign roles at tenant level
python3 src/main.py --users users_export.csv --roles roles_export.csv \
                    --tenant my-tenant-id
```

### Staff API limitations
The Shopify GraphQL `staffMembers` query requires a Shopify **Plus or Advanced** plan and a manually approved `read_users` scope. Additionally, the API returns no role or permission data for staff. For these reasons, **staff users and roles can only be sourced from CSV exports** — `--from-api` applies to customers only.

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

**Getting Shopify credentials (customer migration API mode only):**

`SHOPIFY_SHOP_URL` is your store's `.myshopify.com` domain — no `https://` prefix.

For the access token there are two options. **Option 1 (static token):** if you already have a Shopify Admin API token, set `SHOPIFY_ACCESS_TOKEN` directly and leave the OAuth fields blank. **Option 2 (OAuth flow, recommended):** create an app in the [Shopify Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard) with the `read_customers` scope, add `http://localhost:3000/callback` as an allowed redirect URI, paste the Client ID and Client secret into `.env`, and leave `SHOPIFY_ACCESS_TOKEN` blank. Run the script with `--from-api` and it will open your browser, complete the flow, and save the token automatically.

### 5. Export your Shopify data
##### **Note:** Shopify role and user exports put the CSV file in a ZIP archive, so make sure to unzip it before running this script

From your Shopify Admin:

| Export | Location | Notes |
|---|---|---|
| `customers_export.csv` | Customers → Export → Plain CSV file | Capped at 15 MB, so export in multiple parts if needed. Not needed with `--from-api`. |
| `users_export.csv` | Settings → Users → Export | Required when migrating staff (`--users`). |
| `roles_export.csv` | Settings → Users → Roles → Export | Optional. Only used with `--roles` (requires `--users`). |

---

## Running the Migration

Staff and customer migration are each optional — pass the relevant flags to select which populations to migrate. At least one must be chosen.

`--roles` is optional even when migrating staff. If omitted, staff accounts are created with no roles in Descope — useful if you'd rather assign roles manually. `--roles` cannot be used without `--users` since Shopify roles only apply to staff.

### Dry run (recommended first)

Preview what will be migrated without making any changes:

```bash
# Staff + customers (CSV)
python3 src/main.py --users shopify-exports/users_export.csv \
                    --roles shopify-exports/roles_export.csv \
                    --customers shopify-exports/customers_export.csv \
                    --dry-run

# Staff only
python3 src/main.py --users shopify-exports/users_export.csv \
                    --roles shopify-exports/roles_export.csv \
                    --dry-run

# Customers only (API)
python3 src/main.py --from-api --dry-run
```

### Live migration

```bash
# Staff only
python3 src/main.py --users shopify-exports/users_export.csv \
                    --roles shopify-exports/roles_export.csv

# Customers only — single CSV
python3 src/main.py --customers shopify-exports/customers_export.csv

# Customers only — multiple CSVs (15 MB export cap workaround)
python3 src/main.py --customers shopify-exports/customers_1.csv \
                                shopify-exports/customers_2.csv

# Customers only — from Shopify GraphQL API
python3 src/main.py --from-api

# Staff + customers — customers from CSV
python3 src/main.py --users shopify-exports/users_export.csv \
                    --roles shopify-exports/roles_export.csv \
                    --customers shopify-exports/customers_export.csv

# Staff + customers — customers from API
python3 src/main.py --users shopify-exports/users_export.csv \
                    --roles shopify-exports/roles_export.csv \
                    --from-api
```

Add `--verbose` / `-v` to print each user as it is processed.

### Example output

```
Loading roles from CSV...
Loading staff users from CSV...
Fetching customers from Shopify GraphQL API...

Loaded: 8 roles, 4 staff users, 523 customers.

Ensuring custom attributes exist in Descope...
Starting migration of 9 roles...
Starting migration of 4 staff users...
Starting migration of 523 customers...
Still working, migrated 50 customers...
...

============================================================
MIGRATION SUMMARY
============================================================

── Roles & Permissions ──────────────────────────────────
  Total roles processed : 9
  Created               : 9
  Already existed       : 0
  Failed                : 0
  Permissions created   : 142
  Permissions skipped   : 0
  Permissions failed    : 0

── Staff Users ──────────────────────────────────────────
  Total staff processed : 4
  Created               : 4
  Already existed       : 0
  Failed                : 0
  Role assignments failed: 0

── Customers ────────────────────────────────────────────
  Total customers processed : 523
  Created                   : 522
  Merged into existing user : 1
  Failed                    : 0

Migration complete. Full log written to logs/
============================================================
```

---

## What Gets Migrated

### Base tagging roles
Before any users are migrated, two permission-free tagging roles are created in Descope (or verified if they already exist):

- **"Staff"** — assigned to every migrated staff user
- **"Customer"** — assigned to every migrated customer

These are always created for whichever populations you're migrating, regardless of whether `--roles` is provided. They let you filter users by type in the Descope console.

### Roles
If `--roles` is provided, all Shopify roles are created in Descope with their permissions, then assigned to the relevant staff users alongside the "Staff" tagging role. If `--roles` is omitted, staff accounts are still tagged with "Staff" but receive no Shopify-specific roles.

### Staff users
Each staff member is created as a Descope user with:
- Email as login ID
- The **"Staff"** tagging role plus any Shopify roles from `--roles` assigned
- Active/Pending/Inactive status preserved
- Custom attributes: `shopify_source` = `"staff"`, `shopify_user_type` (value passed through from Shopify)

### Customers
Each customer is created as a Descope user with:
- Email as primary login ID; phone as additional login ID (if present)
- The **"Customer"** tagging role assigned
- Custom attributes: `shopify_source`, `shopify_customer_id`, `shopify_total_spent`, `shopify_total_orders`, `shopify_tags`

### Overlap (staff member who is also a customer)
If the same email appears in both exports, the user is created once (as staff) and the customer attributes are merged into the same record. Both "Customer" and their staff roles are assigned.

> **Note:** The merged user's activation state is left as-is from the staff migration. If the staff member had a Pending or Inactive status in Shopify (and was therefore created as deactivated in Descope), they will remain deactivated after the customer attributes are merged in. Check the Descope console after migration and manually reactivate any such accounts.

### Custom attributes
The following custom attributes are automatically created in your Descope project before migration begins:

| Attribute | Type | Set on |
|---|---|---|
| `shopify_source` | String | All users (`"staff"` or `"customer"`) |
| `shopify_user_type` | String | Staff only (`"Admin"`, `"Point of sale"`, etc. — whatever Shopify exports) |
| `shpfy_needs_contact` | Boolean | Staff and customers — `true` if account has a placeholder login ID |
| `shopify_customer_id` | String | Customers |
| `shopify_total_spent` | String | Customers |
| `shopify_total_orders` | String | Customers |
| `shopify_tags` | String | Customers |

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
