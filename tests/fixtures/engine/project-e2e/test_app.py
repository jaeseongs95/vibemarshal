import unittest

from app import add


class AddTests(unittest.TestCase):
    def test_add_returns_sum(self) -> None:
        self.assertEqual(5, add(2, 3))
