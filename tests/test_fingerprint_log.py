import unittest


class GroupedFingerprintTests(unittest.TestCase):
    def test_groups_of_four_like_the_app(self):
        from bridge.server import grouped_fingerprint
        self.assertEqual(grouped_fingerprint("ab12" * 16), " ".join(["ab12"] * 16))


if __name__ == "__main__":
    unittest.main()
