import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from windowing import collapse_windows


class CollapseWindowsTests(unittest.TestCase):
    def test_adjacent_inclusive_windows_merge(self):
        self.assertEqual(collapse_windows([(2, 4), (5, 8)]), [(2, 8)])

    def test_input_order_is_not_mutated(self):
        source = [(8, 9), (1, 2)]
        original = list(source)
        collapse_windows(source)
        self.assertEqual(source, original)

    def test_overlap_still_merges(self):
        self.assertEqual(collapse_windows([(1, 4), (3, 7)]), [(1, 7)])


if __name__ == "__main__":
    unittest.main()
