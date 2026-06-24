import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRAPER_DIR = os.path.join(ROOT, "scraper")
sys.path.insert(0, SCRAPER_DIR)

from AliExpress import AliExpress


class AliExpressDescriptionImagesTest(unittest.TestCase):
    def test_extracts_images_recursively_from_description_payload(self):
        scraper = AliExpress("https://ko.aliexpress.com/item/1.html", skip_media=True)
        payload = {
            "moduleList": [
                {"type": "text", "data": {"content": "ignored"}},
                {"type": "image", "data": {"url": "https://example.com/a.jpg"}},
                {"type": "custom", "data": {"items": [{"imageUrl": "//example.com/b.webp?x=1"}]}},
            ],
            "extra": '<img data-src="https://example.com/c.png"><div style="background:url(https://example.com/d.jpeg)"></div>',
        }

        self.assertEqual(
            scraper._extract_desc_image_urls_from_payload(payload),
            [
                "https://example.com/a.jpg",
                "https://example.com/b.webp?x=1",
                "https://example.com/c.png",
                "https://example.com/d.jpeg",
            ],
        )

    def test_extracts_all_description_urls_from_intercepted_api_data(self):
        scraper = AliExpress("https://ko.aliexpress.com/item/1.html", skip_media=True)
        scraper._intercepted_data = {
            "mtop.aliexpress.pdp.pc.query": {
                "data": {
                    "result": {
                        "DESC": {
                            "nativeDescUrl": "https://example.com/native.json",
                            "pcDescUrl": "https://example.com/pc.htm",
                            "msiteDescUrl": "https://example.com/msite.htm",
                        }
                    }
                }
            }
        }

        self.assertEqual(
            scraper._extract_desc_urls(),
            [
                "https://example.com/native.json",
                "https://example.com/pc.htm",
                "https://example.com/msite.htm",
            ],
        )

    def test_dedupes_aliexpress_resized_versions_of_same_detail_image(self):
        scraper = AliExpress("https://ko.aliexpress.com/item/1.html", skip_media=True)
        payload = {
            "moduleList": [
                {"type": "image", "data": {"url": "https://ae01.alicdn.com/kf/Sabc123.jpg"}},
            ],
            "html": '<img src="https://ae01.alicdn.com/kf/Sabc123.jpg_640x640q90.jpg">',
        }

        self.assertEqual(
            scraper._extract_desc_image_urls_from_payload(payload),
            ["https://ae01.alicdn.com/kf/Sabc123.jpg"],
        )

    def test_waits_for_late_rendered_description_images(self):
        class DelayedDescriptionPage:
            def __init__(self):
                self.detail_checks = 0

            def evaluate(self, script):
                if "window.scroll" in script:
                    return None
                if "querySelectorAll" in script:
                    self.detail_checks += 1
                    if self.detail_checks == 1:
                        return []
                    return ["https://ae01.alicdn.com/kf/Sdetail123.jpg"]
                return None

        scraper = AliExpress("https://ko.aliexpress.com/item/1.html", skip_media=True)
        scraper.page = DelayedDescriptionPage()
        scraper._DESC_WAIT_SECONDS = 0.05
        scraper._DESC_POLL_SECONDS = 0.01

        self.assertEqual(
            scraper._fetch_desc_images(),
            ["https://ae01.alicdn.com/kf/Sdetail123.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
