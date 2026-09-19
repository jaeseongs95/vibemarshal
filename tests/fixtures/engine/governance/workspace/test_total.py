import unittest

import app


class TotalTests(unittest.TestCase):
    def test_total_uses_add(self) -> None:
        self.assertEqual(6, app.total([1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
