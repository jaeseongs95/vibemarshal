import unittest

import shipping_rules as rules
from app import shipping_fee


class ShippingFeeTests(unittest.TestCase):
    def test_small_order_pays_base_and_weight(self) -> None:
        expected = rules.BASE_FEE_WON + 3 * rules.PER_KG_WON
        self.assertEqual(expected, shipping_fee(3, rules.FREE_SHIPPING_MIN_WON - 1))

    def test_large_order_ships_free(self) -> None:
        self.assertEqual(0, shipping_fee(3, rules.FREE_SHIPPING_MIN_WON))

    def test_non_positive_weight_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shipping_fee(0, 1000)


if __name__ == "__main__":
    unittest.main()
