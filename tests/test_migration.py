"""
tests/test_migration.py

Unit tests for shopify_parser and shopify_client normalization logic.
Migration_utils is not tested here because it requires live Descope credentials;
use a dedicated integration test environment for that.

Run with:
  python3 -m unittest tests.test_migration
"""

import csv
import sys
import os
import unittest

# Allow imports from src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shopify_parser
from shopify_client import _normalize_customer


# ── shopify_parser.parse_customers tests ─────────────────────────────────────

class TestParseCustomers(unittest.TestCase):

    def _write_csv(self, rows: list[dict], path: str) -> None:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def _csv_path(self, name="customers.csv"):
        return os.path.join(self.tmpdir, name)

    def test_basic_customer_parsed(self):
        path = self._csv_path()
        self._write_csv([{
            "Customer ID": "123",
            "First Name": "Alice",
            "Last Name": "Smith",
            "Email": "alice@example.com",
            "Phone": "+15551234567",
            "Total Spent": "99.99",
            "Total Orders": "3",
            "Tags": "vip",
            "Note": "Good customer",
        }], path)
        result = shopify_parser.parse_customers([path])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["email"], "alice@example.com")
        self.assertEqual(result[0]["phone"], "+15551234567")
        self.assertEqual(result[0]["given_name"], "Alice")
        self.assertEqual(result[0]["shopify_customer_id"], "123")
        self.assertEqual(result[0]["total_spent"], "99.99")
        self.assertEqual(result[0]["tags"], "vip")

    def test_customer_without_phone(self):
        path = self._csv_path()
        self._write_csv([{
            "Customer ID": "456",
            "First Name": "Bob",
            "Last Name": "",
            "Email": "bob@example.com",
            "Phone": "",
            "Total Spent": "0.00",
            "Total Orders": "0",
            "Tags": "",
            "Note": "",
        }], path)
        result = shopify_parser.parse_customers([path])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["phone"])
        self.assertIsNone(result[0]["family_name"])

    def test_customer_without_email_or_phone_kept(self):
        """Customers with no contact info are kept by the parser so process_customers can count them as skipped."""
        path = self._csv_path()
        self._write_csv([{
            "Customer ID": "789",
            "First Name": "Ghost",
            "Last Name": "User",
            "Email": "",
            "Phone": "",
            "Total Spent": "0.00",
            "Total Orders": "0",
            "Tags": "",
            "Note": "",
        }], path)
        result = shopify_parser.parse_customers([path])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["email"])
        self.assertIsNone(result[0]["phone"])
        self.assertEqual(result[0]["shopify_customer_id"], "789")

    def test_deduplication_across_files(self):
        row = {
            "Customer ID": "111",
            "First Name": "Carol",
            "Last Name": "Jones",
            "Email": "carol@example.com",
            "Phone": "",
            "Total Spent": "0.00",
            "Total Orders": "0",
            "Tags": "",
            "Note": "",
        }
        path1 = self._csv_path("c1.csv")
        path2 = self._csv_path("c2.csv")
        self._write_csv([row], path1)
        self._write_csv([row], path2)
        result = shopify_parser.parse_customers([path1, path2])
        self.assertEqual(len(result), 1)

    def test_multiple_files_combined(self):
        row1 = {
            "Customer ID": "1", "First Name": "A", "Last Name": "",
            "Email": "a@example.com", "Phone": "", "Total Spent": "0",
            "Total Orders": "0", "Tags": "", "Note": "",
        }
        row2 = {
            "Customer ID": "2", "First Name": "B", "Last Name": "",
            "Email": "b@example.com", "Phone": "", "Total Spent": "0",
            "Total Orders": "0", "Tags": "", "Note": "",
        }
        path1 = self._csv_path("c1.csv")
        path2 = self._csv_path("c2.csv")
        self._write_csv([row1], path1)
        self._write_csv([row2], path2)
        result = shopify_parser.parse_customers([path1, path2])
        self.assertEqual(len(result), 2)


# ── shopify_client._normalize_customer tests ──────────────────────────────────

class TestNormalizeCustomer(unittest.TestCase):

    def _node(self, **kwargs):
        base = {
            "id": "gid://shopify/Customer/12345",
            "firstName": "Alice",
            "lastName": "Smith",
            "defaultEmailAddress": {"emailAddress": "alice@example.com"},
            "defaultPhoneNumber": {"phoneNumber": "+15551234567"},
            "numberOfOrders": "3",
            "amountSpent": {"amount": "99.99", "currencyCode": "USD"},
            "tags": ["vip", "loyal"],
            "note": "Good customer",
            "state": "ENABLED",
        }
        base.update(kwargs)
        return base

    def test_gid_stripped(self):
        result = _normalize_customer(self._node())
        self.assertEqual(result["shopify_customer_id"], "12345")

    def test_email_and_phone_extracted(self):
        result = _normalize_customer(self._node())
        self.assertEqual(result["email"], "alice@example.com")
        self.assertEqual(result["phone"], "+15551234567")

    def test_tags_list_joined(self):
        result = _normalize_customer(self._node())
        self.assertEqual(result["tags"], "vip, loyal")

    def test_no_phone_returns_none(self):
        result = _normalize_customer(self._node(defaultPhoneNumber=None))
        self.assertIsNone(result["phone"])

    def test_no_email_returns_none(self):
        result = _normalize_customer(self._node(defaultEmailAddress=None))
        self.assertIsNone(result["email"])

    def test_amounts_stringified(self):
        result = _normalize_customer(self._node())
        self.assertEqual(result["total_spent"], "99.99")
        self.assertEqual(result["total_orders"], "3")


if __name__ == "__main__":
    unittest.main()
