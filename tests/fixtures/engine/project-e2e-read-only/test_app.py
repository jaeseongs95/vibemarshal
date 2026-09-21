import unittest

from app import add, total


class AppTests(unittest.TestCase):
    def test_add_returns_sum(self) -> None:
        self.assertEqual(5, add(2, 3))

    def test_total_accumulates_with_add(self) -> None:
        self.assertEqual(6, total([1, 2, 3]))
