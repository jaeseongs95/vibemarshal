import unittest

from text import shout


class ShoutTests(unittest.TestCase):
    def test_shout(self) -> None:
        self.assertEqual("HI!", shout("hi"))


if __name__ == "__main__":
    unittest.main()
