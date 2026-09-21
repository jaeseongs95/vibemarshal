import unittest
from unittest import mock

import invoice


class TotalTests(unittest.TestCase):
    def test_sums_prices_in_cents(self) -> None:
        self.assertEqual(375, invoice.total(["1.50", "2.25"]))

    def test_empty_invoice_is_zero(self) -> None:
        self.assertEqual(0, invoice.total([]))

    def test_reuses_money_cents(self) -> None:
        with mock.patch.object(invoice, "cents", wraps=invoice.cents) as spy:
            invoice.total(["1.00", "2.00"])
        self.assertEqual(2, spy.call_count)


if __name__ == "__main__":
    unittest.main()
