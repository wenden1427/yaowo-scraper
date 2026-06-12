import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRAPER_DIR = os.path.join(ROOT, "scraper")
sys.path.insert(0, SCRAPER_DIR)

from AliExpress import AliExpress


class AliExpressApiVariantsTest(unittest.TestCase):
    def test_parse_api_data_maps_sku_paths_to_sku_price_info(self):
        scraper = AliExpress("https://ko.aliexpress.com/item/1005011615735274.html", skip_media=True)
        api_data = {
            "data": {
                "result": {
                    "GLOBAL_DATA": {"globalData": {"subject": "rack"}},
                    "PRODUCT_TITLE": {"text": "rack"},
                    "PRICE": {
                        "skuPriceInfoMap": {
                            "12000056069963449": {"salePriceLocal": "₩11,930|11930|", "salePriceString": "₩11,930"},
                            "12000056069963450": {"salePriceLocal": "₩11,930|11930|", "salePriceString": "₩11,930"},
                            "12000056069963451": {"salePriceLocal": "₩13,999|13999|", "salePriceString": "₩13,999"},
                            "12000056069963452": {"salePriceLocal": "₩13,999|13999|", "salePriceString": "₩13,999"},
                        }
                    },
                    "SKU": {
                        "skuPaths": [
                            {"skuAttr": "14:366#small color A", "skuIdStr": "12000056069963449", "skuStock": 49},
                            {"skuAttr": "14:193#small color B", "skuIdStr": "12000056069963450", "skuStock": 49},
                            {"skuAttr": "14:175#large color A", "skuIdStr": "12000056069963451", "skuStock": 43},
                            {"skuAttr": "14:10#large color B", "skuIdStr": "12000056069963452", "skuStock": 47},
                        ],
                        "skuProperties": [
                            {
                                "skuPropertyId": 14,
                                "skuPropertyName": "색상",
                                "skuPropertyValues": [
                                    {"propertyValueIdLong": 366, "propertyValueDisplayName": "small color A", "skuPropertyImagePath": "small-a.jpg"},
                                    {"propertyValueIdLong": 193, "propertyValueDisplayName": "small color B", "skuPropertyImagePath": "small-b.jpg"},
                                    {"propertyValueIdLong": 175, "propertyValueDisplayName": "large color A", "skuPropertyImagePath": "large-a.jpg"},
                                    {"propertyValueIdLong": 10, "propertyValueDisplayName": "large color B", "skuPropertyImagePath": "large-b.jpg"},
                                ],
                            }
                        ],
                    },
                }
            }
        }

        parsed = scraper._parse_api_data(api_data)

        self.assertEqual(
            [(v["color"], v["price"]) for v in parsed["variants"]],
            [
                ("small color A", "11930"),
                ("small color B", "11930"),
                ("large color A", "13999"),
                ("large color B", "13999"),
            ],
        )

    def test_remove_variant_images_from_gallery_keeps_main_images_only(self):
        scraper = AliExpress("https://ko.aliexpress.com/item/1005011615735274.html", skip_media=True)
        result = {
            "images": [
                "https://ae-pic-a1.aliexpress-media.com/kf/main-1.jpg",
                "https://ae-pic-a1.aliexpress-media.com/kf/variant-a.jpg",
                "https://ae-pic-a1.aliexpress-media.com/kf/main-2.jpg",
                "https://ae-pic-a1.aliexpress-media.com/kf/variant-b.jpg_120x120.jpg_.webp",
            ],
            "variants": [
                {"color": "A", "color_image": "https://ae-pic-a1.aliexpress-media.com/kf/variant-a.jpg"},
                {"color": "B", "color_image": "https://ae-pic-a1.aliexpress-media.com/kf/variant-b.jpg"},
            ],
        }

        scraper._remove_variant_images_from_gallery(result)

        self.assertEqual(
            result["images"],
            [
                "https://ae-pic-a1.aliexpress-media.com/kf/main-1.jpg",
                "https://ae-pic-a1.aliexpress-media.com/kf/main-2.jpg",
            ],
        )
        self.assertEqual(
            result["variant_images"],
            [
                "https://ae-pic-a1.aliexpress-media.com/kf/variant-a.jpg",
                "https://ae-pic-a1.aliexpress-media.com/kf/variant-b.jpg",
            ],
        )


if __name__ == "__main__":
    unittest.main()
