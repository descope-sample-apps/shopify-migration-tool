# Shopify → Descope Migration Tool

A guide for migrating your Shopify customers to Descope. There are 2 ways to migrate:
1. [Running a script for batch migration using using the Shopify API or CSV exports](#batch-migration-script)
2. [Just in time migration using the Shopify API in a Descope flow](#just-in-time-migration-guide)

> **Note:** with just in time migration, the first time a user logs into your store after enabling the migration they will have to do so using email because Shopify doesn't allow phone numbers as login IDs. If they have a phone number associated with their account on Shopify it can be verified and added as a login ID for future logins.

---

## Adding Descope as an OIDC Identity Provider in Shopify
Follow the [Descope with Shopify Plus guide](https://docs.descope.com/getting-started/web-development-platforms/shopify). Make sure to check the `Force Authentication` checkbox and set the Client Authentication type to `Access Key` in the Shopify federated app settings in Descope.

---

## Batch Migration Script

### ⚠️ Important Notes

#### No passwords
Shopify does not export user passwords. All migrated users will have no credentials in Descope. You'll need to configure a Descope flow (magic link, OTP, etc.) for users to authenticate for the first time after migration, after which they can set a new password if you wish to allow so.

#### Customer CSV export cap (15 MB)
Shopify caps customer CSV exports at **15 MB**. For stores with large customer lists:
- Export multiple CSVs using Shopify's segment or date-range filters, then pass them all to `--customers` — duplicates are automatically deduplicated
- Or use `--from-api` which fetches all customers via the GraphQL API with automatic pagination (recommended for large stores)

#### Accounts with no contact info
Shopify allows creating customer accounts with only a first and last name — no email or phone. Since Descope requires a login ID and there's no safe fallback, these accounts are **skipped** during migration. They will appear in the log file as skipped entries.

---

### Setup

#### 1. Clone the repo

```bash
git clone https://github.com/descope/descope-shopify-migration.git
cd descope-shopify-migration
```

#### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

#### 3. Install dependencies

```bash
pip3 install -r requirements.txt
```

#### 4. Configure environment variables

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
# add the read_customers score and http://localhost:3000/callback as an allowed redirect URI, and paste the
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

**Getting Shopify credentials (`--from-api` mode only):**

`SHOPIFY_SHOP_URL` is your store's `.myshopify.com` domain — no `https://` prefix.

For the access token there are two options:
- **Option 1 (static token):** if you already have a Shopify Admin API token, set `SHOPIFY_ACCESS_TOKEN` directly and leave the OAuth fields blank.
- **Option 2 (OAuth flow, recommended):** create an app in the [Shopify Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard) with the `read_customers` scope, add `http://localhost:3000/callback` as an allowed redirect URI and add the app to your store, paste the Client ID and Client secret into `.env`, and leave `SHOPIFY_ACCESS_TOKEN` blank. Run the script with `--from-api` and it will open your browser, complete the flow, and save the token automatically.

> ⚠️ **Note:** If your store is part of the Shopify partner program, you must request access from Shopify to access protected customer data in order to use the API pathway. There is a guide available in the Shopify documentation [here](https://shopify.dev/docs/apps/launch/protected-customer-data#request-access-to-protected-customer-data).

#### 5. Export your Shopify data (CSV Mode Only)

From your Shopify Admin: **Customers → Export → Plain CSV file**

Shopify caps the export at 15 MB. For large stores, export in multiple parts using segment or date filters, then pass all files to `--customers`. Alternatively, use `--from-api` to fetch all customers directly via the GraphQL API with automatic pagination.

#### 6. Create the `shopifyTags` custom attribute in Descope

Before running the migration, create the `shopifyTags` attribute manually in [Descope Console → Users → Custom Attributes](https://app.descope.com/users/attributes):

| Attribute name | Type |
|---|---|
| `shopifyTags` | Multi Select |

Add an option for each tag used in your Shopify store. Tags will not be migrated correctly if this attribute does not exist before the script runs.

The other Shopify attributes (`shopifyCustomerId`, `shopifyTotalSpent`, `shopifyTotalOrders`, `shopifyNote`) are created automatically by the script.

---

### Running the Migration

#### Dry run (recommended first)

Preview what will be migrated without making any changes:

```bash
# From CSV
python3 src/main.py --customers shopify-exports/customers_export.csv --dry-run

# From API
python3 src/main.py --from-api --dry-run
```

#### Live migration

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

#### Example output

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
  Created               : 520
  Merged into existing  : 2
  Skipped (no login ID) : 1
  Failed                : 0

Migration complete. Full log written to logs/
============================================================
```

---

### What Gets Migrated

#### Customers
Each customer is created as a Descope user with:
- Email as primary login ID; phone as additional login ID (if present)
- Active status
- Custom attributes from Shopify (see table below)

Customers with no email and no phone are skipped — they can't be given a login ID.

#### Custom attributes
The following custom attributes are automatically created in your Descope project before migration begins:

| Attribute name | Type | Additional Notes |
|---|---|---|
| `shopifyCustomerId` | Numeric | Shopify customer ID |
| `shopifyTotalSpent` | Numeric | Lifetime spend value |
| `shopifyTotalOrders` | Numeric | Total order count |
| `shopifyTags` | Multi Select | Customer tags from Shopify |
| `shopifyNote` | Text | Internal Shopify note |

#### Existing Descope users
If a customer's email already exists in Descope (e.g. a pre-existing user), the migration merges in the Shopify custom attributes and, if the Shopify record has a phone number not already on the account, adds it as an additional login ID. The account's activation state and all other existing data are left untouched.

---

### Testing

```bash
python3 -m unittest tests.test_migration
```

---

### Logs

A timestamped log file is written to `logs/migration_log_<timestamp>.log` on each run. Failed users are listed in both the log and the terminal summary.

---

## Just In Time Migration Guide

Instead of migrating all users upfront, JIT migration moves users to Descope on their first login. When a user authenticates and doesn't exist in Descope yet, the flow looks them up in Shopify, lets them go through the normal credential check, then applies their Shopify attributes to the newly created account.

> **Note:** Shopify customers with no email and no phone cannot be migrated — Descope requires a login identifier. These users are silently skipped.

### How It Works

```
User enters email or phone
    ↓
Credential check happens normally
    ↓
Already migrated?
    ├─ Yes → end of flow
    └─ No  → Shopify is queried by email or phone
                ↓
           User exists in Shopify?
                ├─ No  → mark user as migrated → end of flow
                └─ Yes → add Shopify attribute values to the user
                              ↓
                         mark user as migrated → end of flow
```

### Prerequisites

- A Descope project
- A Shopify Admin API access token with `read_customers` scope (can be generated using the batch migration script in `--dry-run` mode with the `--from-api` option)

### Setup

#### 1. Create the Shopify HTTP Connector

In Descope Console → Connectors → HTTP, create a new connector with:

- **Base URL:** `https://<your-store>.myshopify.com/admin/api/2026-07/graphql.json`
- **Headers:**
  - `X-Shopify-Access-Token: <your token>` (store as a secret)
  - `Content-Type: application/json`

#### 2. Create Custom Attributes in Descope

Before any users are provisioned, create the following custom attributes in Descope Console → User Management → Custom Attributes:

| Attribute name | Type | Additional Notes |
|---|---|---|
| `shopifyCustomerId` | Numeric |  |
| `shopifyTotalSpent` | Numeric |  |
| `shopifyTotalOrders` | Numeric |  |
| `shopifyTags` | Multi Select | Make sure to create all of the tags you have in Shopify |
| `shopifyNote` | Text |  |
| `migrateFromShopify` | Boolean |  |

#### 3. Configure the Descope Flow

**Step 1 — Check if the user has already been migrated**

Open the login flow you want to add JIT migration to in Descope Console → Flows. Locate the point after the user's credentials are checked (likely at the end of the flow), and add a conditional that checks if `user.customAttributes.migratedFromShopify` is true. The `True` Branch should skip the migration logic.

**Step 2 — Build the Shopify query string (Scriptlet action)**

Add a **Scriptlet** action. Shopify's customer search API requires an `email:` or `phone:` prefix on the search term, so this step constructs the right query string before calling Shopify.

Add an argument `loginId` with value `form.externalId` and put the following in the script code:

```javascript
return {
    shopifyQuery: loginId.startsWith("+") ? `phone:${loginId}` : `email:${loginId}`
};
```

The output key `shopifyQuery` is referenced in the next step.

**Step 3 — Look up the customer in Shopify (HTTP Connector action)**

Add an **HTTP Connector** POST action and select the Shopify connector created in step 1. Configure it with the following payload. The scriptlet will substitute the `shopifyQuery` output from the previous scriptlet step into `{{scripts.scriptletResult.shopifyQuery}}`:

```json
{
  "query": "query($q: String!) { customers(first: 1, query: $q) { edges { node { id firstName lastName defaultEmailAddress { emailAddress } defaultPhoneNumber { phoneNumber } numberOfOrders amountSpent { amount } tags note } } } }",
  "variables": { "q": "{{scripts.scriptletResult.shopifyQuery}}" }
}
```

**Step 4 - Parse the Shopify response data**

Add a **Scriptlet** action. Shopify's API returns customer data in an array within JSON, which is easier to parse using JavaScript. Set the context key to `parsedShopify` and put the following as the script code:

```javascript
const customer = shopifyResult?.data?.customers?.edges?.[0]?.node;

if (!customer) {
  return {
    found: false,
    customerId: "",
    email: "",
    phone: "",
    firstName: "",
    lastName: "",
    numberOfOrders: "",
    amountSpent: "",
    tags: "",
    note: ""
  };
}

return {
  found: true,
  customerId: Number(customer.id.split("/").pop()) || 0,
  email: customer.defaultEmailAddress?.emailAddress || "",
  phone: customer.defaultPhoneNumber?.phoneNumber || "",
  firstName: customer.firstName || "",
  lastName: customer.lastName || "",
  numberOfOrders: Number(String(customer.numberOfOrders ?? "0")),
  amountSpent: Number(String(customer.amountSpent?.amount ?? "0")),
  tags: Array.isArray(customer.tags) ? customer.tags.join(",") : "",
  note: customer.note || ""
};
```

After this step, add a condition: if `scripts.parsedShopify.found` is false, skip ahead past applying the Shopify attributes to marking the user as migrated. The user doesn't have a Shopify account.

**Step 5 — Apply Shopify attributes (Update User / Attributes action)**

After the credential check, add an **Update User / Attributes** action. Map each attribute from the Shopify connector response (step 2) to the corresponding custom attribute on the user:

| Attribute | Source value |
|---|---|
| `shopifyCustomerId` | `scripts.parsedShopify.customerId` |
| `shopifyTotalSpent` | `scripts.parsedShopify.amountSpent` |
| `shopifyTotalOrders` | `scripts.parsedShopify.numberOfOrders` |
| `shopifyTags` | `scripts.parsedShopify.tags` |
| `shopifyNote` | `scripts.parsedShopify.note` |
| `Phone` | `scripts.parsedShopify.phone` |

**Step 6 — Mark the user as migrated**

Add another **Update User / Attributes** action. Choose `migratedFromShopify` for the user attribute field, set the type to `Boolean`, and set the value to `True`.

**[Optional] Step 7 — Verify migrated phone numbers**

Since Shopify allows users to have unverified phone numbers, we can't just accept migrated phone numbers and immediately add them as login IDs. Instead, we can use OTP (or any other phone based authentication method like [magic link](https://docs.descope.com/auth-methods/magic-link), [enchanted link](https://docs.descope.com/auth-methods/enchanted-link), or [nOTP](https://docs.descope.com/auth-methods/notp)) to verify the number first and then add it as a login ID.

To implement this as part of you JiT migration flow, add a condition right after the `Update User / Attributes` action that assigns the values from shopify to the user. Check for 2 conditions: `user.phone` is not empty(meaning a phone number has been migrated from Shopify), and that `user.verifiedPhone` is false, meaning the phone number wasn't already verified in case the user already existed in Descope.

If this is false, go to the `Update User / Attributes` action that marks users as migrated. If it's true, add an `Update User` action block with your preferred authentication method, for example `Update User / OTP / SMS`. If you want to allow the user to log in with their phone number as their login ID, make sure to check the **Add to login IDs** box.

---

## License

MIT
