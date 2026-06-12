import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRAPER_DIR = os.path.join(ROOT, "scraper")
sys.path.insert(0, SCRAPER_DIR)

from gui import _split_aliexpress_by_price


class AliExpressPriceGroupingTest(unittest.TestCase):
    def test_groups_from_lowest_remaining_price_and_uses_group_median(self):
        prices = ["10000", "10000", "10050", "20000", "20000", "20100", "30000", "30000"]
        products = [
            {
                "parent_sku": "item123",
                "sku": f"item123_c{i}",
                "color": f"c{i}",
                "price": price,
            }
            for i, price in enumerate(prices, 1)
        ]

        grouped = _split_aliexpress_by_price(products)

        self.assertEqual(
            [p["price"] for p in grouped],
            ["10000", "10000", "10000", "20000", "20000", "20000", "30000", "30000"],
        )
        self.assertEqual(
            [p["parent_sku"] for p in grouped],
            ["item123_1", "item123_1", "item123_1", "item123_2", "item123_2", "item123_2", "item123_3", "item123_3"],
        )


if __name__ == "__main__":
    unittest.main()
