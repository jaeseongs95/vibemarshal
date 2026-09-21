import unittest

from money import cents


class CentsTests(unittest.TestCase):
    def test_dollars_and_cents(self) -> None:
        self.assertEqual(1234, cents("12.34"))

    def test_small_amount(self) -> None:
        self.assertEqual(5, cents("0.05"))

    def test_whole_dollars(self) -> None:
        self.assertEqual(300, cents("3"))


if __name__ == "__main__":
    unittest.main()
