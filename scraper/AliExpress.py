"""
================================================================================
AliExpress Web Scraper — ali_express.py
================================================================================
AliExpress-specific scraper. Inherits browser lifecycle, page loading,
scrolling, media download, snapshot, and description file logic from
BaseScraper. Only keeps platform-specific selectors and extraction methods.

Usage:
    from ali_express import AliExpress
    scraper = AliExpress("https://aliexpress.com/item/...")
    data = scraper.scrape()
"""

import datetime
import json
import os
import re
import sys
from bs4 import BeautifulSoup, Tag
from colorama import Style
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse

from product_utils import normalize_product_name
from base_scraper import (
    BaseScraper,
    BackgroundColors,
    VERBOSE,
    verbose_output,
    output_result,
    verify_filepath_exists,
    verify_dot_env_file,
    calculate_execution_time,
    play_sound,
    SOUND_FILE,
    SOUND_COMMANDS,
    RUN_FUNCTIONS,
)


# =============================================================================
# AliExpress HTML Selectors
# =============================================================================

HTML_SELECTORS = {
    "product_name": [
        ("h1", {"data-pl": "product-title"}),
        ("h1", {}),
        ("div", {"class": re.compile(r".*product.*title.*", re.IGNORECASE)}),
    ],
    "current_price": [
        ("div", {"class": "price-default--currentWrap--A_MNgCG"}),
        ("span", {"class": "price-default--current--F8OlYIo"}),
        ("span", {"class": re.compile(r".*price.*", re.IGNORECASE)}),
    ],
    "old_price": [
        ("span", {"class": "price-default--original--CWcHOit"}),
        ("div", {"class": re.compile(r".*price.*original.*", re.IGNORECASE)}),
        ("span", {"class": re.compile(r".*old.*price.*", re.IGNORECASE)}),
    ],
    "discount": [],
    "description": [
        ("div", {"id": "product-description"}),
        ("div", {"class": re.compile(r".*description.*", re.IGNORECASE)}),
    ],
    "gallery": {"class": "slider--wrap--dfLgmYD"},
    "detail_label": {"class": "specification--title--SfH3sA8"},
    "specs_container": {"class": "specification--list--GZuXzRX"},
    "specs_row": {"class": "specification--line--IXeRJI7"},
    "specs_title": {"class": "specification--title--SfH3sA8"},
    "specs_value": {"class": "specification--desc--Dxx6W0W"},
    "review_images_container": {"class": "filter--bottom--12yws12"},
    "shipping_options": {"class": "vat-installment--item--Fgco36c"},
}

AFFILIATE_URL_PATTERN = (
    r"https?://("
    r"s\.click\.aliexpress\.com/e/[A-Za-z0-9_]+"
    r"|"
    r"(?:www\.)?aliexpress\.[a-z.]+/.*[?&](?:aff_fcid|aff_fsk|aff_trace_key|aff_platform|dp|terminal_id|af)="
    r")"
)


# =============================================================================
# AliExpress Scraper
# =============================================================================

class AliExpress(BaseScraper):
    """AliExpress product scraper with JS extraction + API interception."""

    PLATFORM_NAME = "AliExpress"
    _DESC_WAIT_SECONDS = 8
    _DESC_POLL_SECONDS = 0.5

    # API URL patterns to intercept (in priority order)
    _API_PATTERNS = [
        "mtop.aliexpress.pdp.pc.query",    # Primary: full product data + variants
        "mtop.aliexpress.merchant.pc.detail",
        "aliexpress.com/aeglodetailweb",
    ]

    # Region → locale mapping for URL detection
    _REGION_MAP = {
        "ko.aliexpress.com": "ko_KR",
        "pt.aliexpress.com": "pt_BR",
        "es.aliexpress.com": "es_ES",
        "fr.aliexpress.com": "fr_FR",
        "de.aliexpress.com": "de_DE",
        "ru.aliexpress.com": "ru_RU",
        "ja.aliexpress.com": "ja_JP",
    }

    def _switch_region(self):
        """Switch AliExpress region to match URL locale for correct currency."""
        if self.page is None:
            return
        domain = urlparse(self.product_url).netloc.lower()
        locale = self._REGION_MAP.get(domain)
        if not locale:
            return

        try:
            # Check current currency in page
            current_currency = self.page.evaluate("""() => {
                const m = document.cookie.match(/aep_usuc_f=([^;]+)/);
                if (m) {
                    const parts = decodeURIComponent(m[1]).split('+');
                    return parts[0] || '';
                }
                const currencyEl = document.querySelector('[class*="currency"], [class*="price"]');
                return currencyEl ? (currencyEl.textContent.match(/[₩$€£¥]/) || [''])[0] : '';
            }""")

            # Determine if we need to switch
            need_switch = False
            target_currency = locale.split('_')[1]  # e.g., "KR" from "ko_KR"
            if target_currency in ('KR', 'JP') and locale not in str(current_currency):
                need_switch = True

            if not need_switch:
                return

            # Set locale cookie and reload
            js = f"""() => {{
                // Set the aep_usuc_f cookie to force locale+currency
                var d = new Date();
                d.setTime(d.getTime() + 86400000); // 24h
                var localeParts = '{locale}'.split('_');
                var lang = localeParts[0];
                var region = localeParts[1];
                // Format: region+currency+lang+timezone
                var val = region + '+' + region + '+' + lang + '+09:00';
                document.cookie = 'aep_usuc_f=' + val + ';path=/;domain=.aliexpress.com;expires=' + d.toUTCString();
                document.cookie = 'intl_locale=' + lang + '_' + region + ';path=/;domain=.aliexpress.com;expires=' + d.toUTCString();
                document.cookie = 'intl_common_currency=' + region + ';path=/;domain=.aliexpress.com;expires=' + d.toUTCString();
                return true;
            }}"""
            self.page.evaluate(js)

            # Set cookies — they'll take effect on next page load
            # Don't reload here; the main page load already happened
            verbose_output(
                f"{BackgroundColors.GREEN}Switched region to {locale} ({target_currency}){Style.RESET_ALL}"
            )
        except Exception:
            pass  # Non-critical

    # JS extraction script — multi-region AliExpress selectors
    _JS_EXTRACT = """() => {
        const out = {};
        // 1. Product title
        out.title = (document.title || '').replace(/\\s*[|\\-]\\s*AliExpress(\\s*\\S+)?\\s*$/i, '').trim();
        if (!out.title || out.title.length < 5) {
            const h1 = document.querySelector('h1[data-pl="product-title"], h1');
            if (h1) out.title = (h1.textContent || '').trim();
        }
        // 2. Current price — extract FIRST number only (ignore combined text)
        const priceSels = [
            '[class*="price--current"]', '[class*="price-current"]',
            '[class*="current-price"]', '.product-price-value',
            '[class*="price-kr--current"]',
        ];
        for (const sel of priceSels) {
            const el = document.querySelector(sel);
            if (el) {
                const txt = (el.textContent || '').trim();
                const m = txt.match(/[\\d,.]+/);
                out.currentPrice = m ? m[0] : txt;
                break;
            }
        }
        // 3. Original price — the line-through span inside originWrap
        const oldSels = [
            '[class*="originWrap"] span[style*="line-through"]',
            '[class*="originWrap"] s',
            '[class*="originWrap"] del',
            '[class*="price--original"]', '[class*="price-original"]',
            'del', 's',
        ];
        for (const sel of oldSels) {
            const el = document.querySelector(sel);
            if (el) {
                const txt = (el.textContent || '').trim();
                const m = txt.match(/[\\d,.]+/);
                if (m) { out.oldPrice = m[0]; break; }
            }
        }
        // 3b. Discount/save amount
        const saveSel = '[class*="pricePromotionInfo"], [class*="sale--discount"], [class*="price--discount"]';
        const saveEl = document.querySelector(saveSel);
        if (saveEl) {
            const txt = (saveEl.textContent || '').trim();
            const m = txt.match(/[\\d,.]+/);
            if (m) out.discountAmount = m[0];
        }
        // 5. Gallery images — ONLY main product images, NOT variant swatches
        const imgs = [];
        const seen = new Set();
        const SWATCH_SIZES = ['27x27','48x48','32x32','40x40','50x50','60x60','80x80','100x100','120x120'];
        function isSwatch(src) {
            for (const s of SWATCH_SIZES) { if (src.includes(s)) return true; }
            return false;
        }
        function normalize(src) {
            // Strip size suffixes: _220x220, _960x960q75, _220x220q75.jpg_.avif, etc.
            let s = src.replace(/_[0-9]{2,4}x[0-9]{2,4}.*?(?=\\.(jpg|png|jpeg|webp|avif))/gi, '');
            // Remove .avif wrapper
            s = s.replace(/\\.avif$/i, '');
            // Clean trailing garbage: .jpg_, .jpg.jpg_, .png_, etc.
            s = s.replace(/[._]+$/g, '');
            // Remove duplicate extensions like ".jpg.jpg" → ".jpg"
            s = s.replace(/([.](?:jpg|jpeg|png|webp))\\1+/gi, '$1');
            // Fix if avif removal left a bare _ at the end
            s = s.replace(/_$/g, '');
            if (!s.startsWith('http')) s = 'https:' + s;
            return s;
        }
        // Main image (large display)
        // Slider images — keep all in DOM order, skip only consecutive duplicates
        let prevKey = '';
        document.querySelectorAll(
            '[class*="slider--wrap"] img, [class*="images-view-item"] img, '
            + '[class*="image-list"] img, img[class*="thumbnail"]'
        ).forEach(img => {
            let src = img.src || img.getAttribute('data-src') || '';
            if (!src || src.includes('data:') || isSwatch(src)) return;
            let full = normalize(src);
            if (full && full !== prevKey) { prevKey = full; imgs.push(full); }
        });
        // Fallback: product images (exclude known variant patterns)
        if (imgs.length < 2) {
            document.querySelectorAll('img[src*="aliexpress-media.com/kf/"]').forEach(img => {
                let src = img.src || img.getAttribute('data-src') || '';
                if (!src || src.includes('data:') || isSwatch(src)) return;
                let full = normalize(src);
                if (full && !seen.has(full)) { seen.add(full); imgs.push(full); }
            });
        }
        // 6. Color names and size names — element position + label text combined
        const colors = [];
        const sizes = [];
        const COLOR_RE = /color|colour|색|컬러|색상|컬러|couleur|farbe|cor|colore/i;
        const SIZE_RE = /size|tamanho|사이즈|크기|talle|größe|taille|taglia|talla/i;

        // ── Method A: New layout — sku-item--property header / image swatches / text buttons ──
        // Step 1: Identify which property section is color vs size by label TEXT
        let colorPropEl = null;
        let sizePropEl = null;
        document.querySelectorAll('[class*="sku-item--property"]').forEach(prop => {
            const text = (prop.textContent || '').trim();
            if (!colorPropEl && COLOR_RE.test(text)) {
                colorPropEl = prop;
                // Also extract current color name if shown (e.g. "색상: 카키색" → "카키색")
                const name = text.replace(/^[^:：]*[：:]\\s*/, '').replace(/\\s*\\([^)]*\\)/g, '').trim();
                if (name && name.length < 40 && !COLOR_RE.test(name) && !SIZE_RE.test(name)
                    && !colors.some(c => c.name === name)) {
                    colors.push({name, img: ''});
                }
            } else if (!sizePropEl && SIZE_RE.test(text)) {
                sizePropEl = prop;
                // Try to extract current size value from size property header (e.g. "크기: Large")
                const val = text.replace(/^[^:：]*[：:]\\s*/, '').trim();
                if (val && val.length < 30 && !COLOR_RE.test(val) && !SIZE_RE.test(val)
                    && !sizes.includes(val)) {
                    sizes.push(val);
                }
            }
        });
        // Step 2: Color swatches — image elements (identified by having img children)
        document.querySelectorAll('[class*="sku-item--image"]').forEach(el => {
            const title = (el.getAttribute('title') || '').trim();
            const imgEl = el.querySelector('img');
            const src = imgEl ? (imgEl.src || imgEl.getAttribute('data-src') || '') : '';
            if (title && title.length < 40 && !SIZE_RE.test(title)) {
                if (!colors.some(c => c.name === title)) {
                    colors.push({name: title, img: normalize(src)});
                } else {
                    const existing = colors.find(c => c.name === title);
                    if (existing && !existing.img && src) existing.img = normalize(src);
                }
            }
        });
        // Step 3: Size buttons — text elements, BUT exclude color-related text
        document.querySelectorAll('[class*="sku-item--text"]').forEach(el => {
            const text = (el.textContent || '').trim();
            // Skip if looks like a color name or size label header
            if (text && text.length < 30 && !sizes.includes(text)
                && !COLOR_RE.test(text) && !SIZE_RE.test(text)) {
                sizes.push(text);
            }
        });

        // ── Method B: Old layout — sku-property groups with title + li/swatch children ──
        if (colors.length === 0 && sizes.length === 0) {
            document.querySelectorAll('[class*="sku-property"]').forEach(group => {
                const titleEl = group.querySelector('[class*="sku-property-title"], [class*="sku-title"]');
                const propName = (titleEl ? titleEl.textContent : '').trim();
                const isColor = COLOR_RE.test(propName);
                const isSize = SIZE_RE.test(propName);
                const items = [];
                group.querySelectorAll('li, [class*="sku-item"]').forEach(item => {
                    const img = item.querySelector('img');
                    const name = (item.getAttribute('title') || item.textContent || '').trim();
                    if (name && name.length < 50) {
                        items.push({name, img: img ? (img.src || img.getAttribute('data-src') || '') : ''});
                    }
                });
                if (isColor) {
                    items.forEach(i => colors.push(i));
                } else if (isSize) {
                    items.forEach(i => sizes.push(i.name));
                } else {
                    // Fallback: use combined signal — title text weak match + item characteristics
                    const weakColor = /색|colo|컬러/i.test(propName);
                    const weakSize = /크기|size|tama/i.test(propName);
                    if (weakColor && !weakSize) {
                        items.forEach(i => colors.push(i));
                    } else if (weakSize && !weakColor) {
                        items.forEach(i => sizes.push(i.name));
                    } else {
                        // Last resort: image presence signal (color swatches have images)
                        if (items.some(i => i.img)) {
                            items.forEach(i => colors.push(i));
                        } else {
                            items.forEach(i => sizes.push(i.name));
                        }
                    }
                }
            });
        }

        out.colors = colors;
        out.sizes = sizes;

        // 7. Variant images — collect from DOM swatches directly (full-res from gallery, split later)
        const variantImgs = [];
        const vSeen2 = new Set();
        document.querySelectorAll('[class*="sku-item--image"] img, [class*="sku-property-image"] img').forEach(img => {
            let src = img.src || img.getAttribute('data-src') || '';
            if (src && !vSeen2.has(src)) { vSeen2.add(src); variantImgs.push(normalize(src)); }
        });
        // Keep ALL images as gallery for now — variant split happens after click extraction
        out.images = imgs;
        out.variantImages = variantImgs;

        // 8. Tags — extract spec table + product badges only (skip navigation noise)
        const tags = [];
        // Product badges: sale/promo/shipping labels inside product area
        document.querySelectorAll(
            '[class*="product-"] [class*="tag"], [class*="detail"] [class*="tag"], '
            + '[class*="promotion-"], [class*="sale--"], [class*="ship-"]'
        ).forEach(el => {
            const txt = (el.textContent || '').trim();
            if (txt && txt.length < 40 && txt.length > 1 && !tags.includes(txt)
                && !/북구|USD|KRW|Language/i.test(txt)) {
                tags.push(txt);
            }
        });
        // Spec table: each row has 2 property groups (4 columns total)
        document.querySelectorAll('[class*="specification--line"]').forEach(row => {
            const props = row.querySelectorAll('[class*="specification--prop"]');
            props.forEach(prop => {
                const titleEl = prop.querySelector('[class*="specification--title"] span, [class*="title"]');
                const descEl = prop.querySelector('[class*="specification--desc"] span, [class*="desc"]');
                if (titleEl && descEl) {
                    const k = titleEl.textContent.trim();
                    const v = descEl.textContent.trim();
                    if (k && v && k.length < 50 && v.length < 80) {
                        tags.push(k + ': ' + v);
                        if (/type|style|material|pattern|fit|heel|season/i.test(k)) {
                            if (!tags.includes(v)) tags.push(v);
                        }
                    }
                }
            });
        });
        out.tags = tags;
        // 9. Description / Overview
        let overview = '';
        const descEl = document.querySelector(
            '#product-description, [class*="detail-desc"], '
            + '[class*="description--"]:not([class*="title"])'
        );
        if (descEl) {
            overview = (descEl.innerText || descEl.textContent || '').trim();
        }
        // Fallback: search for overview section content
        if (!overview || overview.length < 10) {
            document.querySelectorAll('[class*="module--"], [class*="panel--"]').forEach(el => {
                const txt = (el.innerText || el.textContent || '').trim();
                if (txt.length > 50 && txt.length < 5000) {
                    overview = txt;
                }
            });
        }
        out.description = overview;
        // 10. Seller
        const sellerEl = document.querySelector('[class*="store-name"], [class*="seller-name"], [class*="shop-name"], a[class*="store"]');
        if (sellerEl) out.sellerName = (sellerEl.textContent || '').trim();
        // 11. Embedded JSON
        if (typeof window.runParams !== 'undefined') out.runParams = JSON.stringify(window.runParams);
        return JSON.stringify(out);
    }"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_patterns = self._API_PATTERNS

    # ── JS Extraction (fast, no DOM wait) ──────────────────────────

    def _check_login(self) -> bool:
        """Return True if we're on a logged-in product page (not login wall)."""
        if self.page is None:
            return False
        try:
            body = self.page.evaluate("document.body ? document.body.innerText : ''")
            login_keywords = ['로그인', '로그인 / 회원가입', 'Sign in', 'Register', 'Login']
            for kw in login_keywords:
                if kw in body[:500]:
                    verbose_output(
                        f"{BackgroundColors.RED}NOT LOGGED IN — page shows login wall.{Style.RESET_ALL}"
                    )
                    return False
            return True
        except Exception:
            return False

    def _wait_for_product(self, timeout: int = 20) -> bool:
        """Wait for product content to actually render on page."""
        if self.page is None:
            return False
        # Scroll to trigger lazy content
        try:
            for _ in range(5):
                self.page.evaluate('window.scrollBy(0, 500)')
                import time; time.sleep(0.5)
        except Exception:
            pass
        # Wait for product title OR price to be non-empty
        try:
            self.page.wait_for_function(
                '''() => {
                    const h1 = document.querySelector('h1');
                    const price = document.querySelector('[class*="price--current"], [class*="price-current"], [class*="current-price"]');
                    return (h1 && h1.textContent.trim().length > 3) || (price && price.textContent.trim().length > 1);
                }''',
                timeout=timeout * 1000,
            )
            return True
        except Exception:
            pass
        return False

    def _expand_overview(self):
        """Click '더보기' buttons to expand spec table and description."""
        if self.page is None:
            return
        import time
        try:
            # Click spec table expand button (first 더보기 inside specification--wrap)
            clicked = self.page.evaluate("""() => {
                const wrap = document.querySelector('[class*=\"specification--wrap\"]');
                if (wrap) {
                    const btn = wrap.querySelector('button, [class*=\"btn\"]');
                    if (btn) { btn.click(); return true; }
                }
                return false;
            }""")
            if clicked:
                time.sleep(0.8)
            # Also click description extend button if present
            self.page.evaluate("""() => {
                const wrap = document.querySelector('[class*=\"extend--wrap\"]');
                if (wrap) {
                    const btn = wrap.querySelector('button, [class*=\"btn\"]');
                    if (btn) btn.click();
                }
            }""")
            time.sleep(0.5)
        except Exception:
            pass

    def _extract_via_js(self) -> Optional[dict]:
        """Extract product data via page.evaluate() — single load, no retries."""
        if self.page is None:
            return None

        # Wait for product content
        if not self._wait_for_product(timeout=20):
            verbose_output(f"{BackgroundColors.YELLOW}Product content didn't load.{Style.RESET_ALL}")
            return None

        # Check login
        if not self._check_login():
            return None

        # Expand collapsed content (spec table + description)
        self._expand_overview()

        try:
            raw = self.page.evaluate(self._JS_EXTRACT)
            jsd = json.loads(raw)
            name = jsd.get("title", "")
            if not name or len(name) < 3:
                return None

            cur = jsd.get("currentPrice", "")
            old = jsd.get("oldPrice", "")
            discount = jsd.get("discount", "")
            overview = jsd.get("overview", "") or ""
            discount_amount = jsd.get("discountAmount", "") or ""

            cur_int, cur_dec = self._parse_price_from_api(cur)
            old_int, old_dec = self._parse_price_from_api(old)

            # If old price not found directly, compute from current + discount
            if (not old_int or old_int == "0") and discount_amount:
                d_int, d_dec = self._parse_price_from_api(discount_amount)
                if d_int and d_int != "0":
                    try:
                        old_int = str(int(cur_int) + int(d_int))
                        old_dec = d_dec
                    except: pass

            # If JS prices are empty, try runParams JSON
            if (not cur_int or cur_int == "0") and jsd.get("runParams"):
                try:
                    rp = json.loads(jsd["runParams"])
                    rp_str = json.dumps(rp)
                    for k in ["price", "salePrice", "originalPrice", "formatedPrice"]:
                        m = re.search(f'"{k}"\\s*:\\s*"([^"]+)"', rp_str)
                        if m:
                            if k in ("price", "salePrice", "formatedPrice"):
                                if not cur_int or cur_int == "0":
                                    cur_int, cur_dec = self._parse_price_from_api(m.group(1))
                            elif k == "originalPrice":
                                old_int, old_dec = self._parse_price_from_api(m.group(1))
                except Exception:
                    pass

            images = jsd.get("images", []) or []       # main gallery
            variant_images = jsd.get("variantImages", []) or []  # color swatches
            description = jsd.get("description", "") or ""
            seller = jsd.get("sellerName", "") or ""
            tags = jsd.get("tags", []) or []

            # Build product_tag: all spec key:value pairs, comma-separated
            # (uploader's category matching uses this as a keyword pool)
            tag_terms = []
            for t in tags:
                parts = t.split(': ', 1)
                if len(parts) == 2:
                    tag_terms.append(parts[1].strip())  # value only (more concise)
            product_tag = ", ".join(tag_terms) if tag_terms else ""

            # Structured color/size from JS (already separated)
            colors = jsd.get("colors", []) or []
            sizes = jsd.get("sizes", []) or []
            if not sizes:
                sizes = ["One Size"]

            # Build variant entries for gui.py
            color_list = []
            color_image_map = {}
            for c in colors:
                cn = c.get("name", "")
                ci = c.get("img", "")
                if cn:
                    color_list.append(cn)
                    if ci:
                        color_image_map[cn] = ci

            verbose_output(
                f"{BackgroundColors.GREEN}JS extraction: name={name[:40]}..., "
                f"price={cur_int}.{cur_dec}, imgs={len(images)}, "
                f"colors={len(colors)}, sizes={len(sizes)}{Style.RESET_ALL}"
            )
            result = {
                "name": name,
                "current_price_integer": cur_int or "0",
                "current_price_decimal": cur_dec or "00",
                "old_price_integer": old_int or "N/A",
                "old_price_decimal": old_dec or "N/A",
                "discount_percentage": str(discount) if discount else "N/A",
                "description": str(description),
                "overview": overview,
                "tags": tags,
                "product_tag": product_tag,
                "url": self.product_url,
                "images": images,
                "variant_images": variant_images,
                "colors": color_list,
                "sizes": sizes,
                "color_images": color_image_map,
                "sku_map": {},
                "seller_name": seller,
                "seller_url": "",
                "is_international": False,
            }
            return result
        except Exception as e:
            verbose_output(f"{BackgroundColors.YELLOW}JS extraction error: {e}{Style.RESET_ALL}")
            return None

    # ── API-First Extraction ────────────────────────────────────────

    # ── Multi-SKU Variant Extraction ──────────────────────────────

    def _extract_variants(self) -> List[dict]:
        """Extract color variants by clicking each swatch. API-first, DOM click, then fallback."""
        # Try API data first
        for pattern in self._API_PATTERNS:
            data = self._intercepted_data.get(pattern)
            if not data:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                variants = self._parse_variants_from_mtop(item)
                if variants:
                    return variants

        # DOM fallback: wait for SKU elements then extract
        if self.page:
            return self._extract_variants_from_dom()
        return []

    def _extract_variants_old_unused(self):"""unused"""
    def _extract_variants_from_dom(self) -> List[dict]:
        """Extract color variants from JS data (colors list). No DOM clicking needed."""
        # JS extraction already provides structured colors — reuse that
        return []  # Colors now handled directly in _extract_via_js and merge flow

    def _extract_variants_click(self) -> List[dict]:
        """Click each color swatch to get color name, sizes, and swatch image."""
        if self.page is None:
            return []
        try:
            import time

            # Find all color swatch elements (uniq by image src)
            swatch_data = self.page.evaluate("""() => {
                const items = [];
                const seen = new Set();
                document.querySelectorAll('[class*="sku-item--image"]').forEach(el => {
                    const img = el.querySelector('img');
                    const src = img ? (img.src || img.getAttribute('data-src') || '') : '';
                    if (src && !seen.has(src)) {
                        seen.add(src);
                        items.push({img: src, isSoldOut: el.className.includes('soldOut')});
                    }
                });
                return JSON.stringify(items);
            }""")
            items = json.loads(swatch_data)
            if not items or len(items) <= 1:
                return []

            # Helper: read current color name from color property element (by TEXT, not position)
            def _read_color_name():
                return self.page.evaluate("""() => {
                    const COLOR_RE = /color|colour|색|컬러|색상|컬러|couleur|farbe|cor|colore/i;
                    const props = document.querySelectorAll('[class*="sku-item--property"]');
                    for (const prop of props) {
                        const txt = (prop.textContent || '').trim();
                        if (COLOR_RE.test(txt)) {
                            // "색상: 카키색" → "카키색"
                            const m = txt.match(/[:：]\\s*(.+)/);
                            const name = m ? m[1].trim() : '';
                            if (name && name.length < 40) return name;
                        }
                    }
                    // Fallback: first property element (might not be color)
                    const first = document.querySelector('[class*="sku-item--property"]');
                    if (!first) return '';
                    const txt = (first.textContent || '').trim();
                    const m = txt.match(/[:：]\\s*(.+)/);
                    return m ? m[1].trim() : txt;
                }""")

            # Helper: read current sizes — only text elements near size section
            def _read_sizes():
                return self.page.evaluate("""() => {
                    const COLOR_RE = /color|colour|색|컬러|색상/i;
                    const SIZE_RE = /size|tamanho|사이즈|크기/i;
                    const sizes = [];
                    document.querySelectorAll('[class*="sku-item--text"]').forEach(el => {
                        const txt = (el.textContent || '').trim();
                        // Skip color names, size headers, and long text
                        if (txt && txt.length < 30 && !sizes.includes(txt)
                            && !COLOR_RE.test(txt) && !SIZE_RE.test(txt)) {
                            sizes.push(txt);
                        }
                    });
                    return sizes;
                }""")

            # Helper: read current sale price (优惠后价格) after swatch click
            def _read_price():
                return self.page.evaluate("""() => {
                    // Try current/discount price selectors first
                    const sels = [
                        '[class*="price--current"]',
                        '[class*="price-current"]',
                        '[class*="currentPrice"]',
                        '[class*="product-price-current"]',
                        '[class*="product-price"]:not([class*="original"])',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        const txt = el ? (el.textContent || '').trim() : '';
                        if (txt && /\\d/.test(txt)) return txt;
                    }
                    return '';
                }""")

            variants = []
            for i, item in enumerate(items):
                try:
                    # Click the i-th swatch
                    clicked = self.page.evaluate(f"""() => {{
                        const els = document.querySelectorAll('[class*="sku-item--image"]');
                        let idx = 0;
                        const seen = new Set();
                        for (const el of els) {{
                            const img = el.querySelector('img');
                            const src = img ? (img.src || img.getAttribute('data-src') || '') : '';
                            if (src && !seen.has(src)) {{
                                if (idx === {i}) {{ el.click(); return true; }}
                                seen.add(src);
                                idx++;
                            }}
                        }}
                        return false;
                    }}""")
                    if not clicked:
                        continue
                    # Wait longer for page to update after click
                    time.sleep(0.6)
                    # Wait for color name to actually update (max 2s)
                    for _ in range(8):
                        name = _read_color_name()
                        if name:
                            break
                        time.sleep(0.25)

                    name = _read_color_name()
                    sizes = _read_sizes()
                    click_price = _read_price()

                    if name:
                        variants.append({
                            "color": name,
                            "color_image": item.get("img", ""),
                            "sizes": sizes,
                            "sold_out": item.get("isSoldOut", False),
                            "main_image": "",
                            "price": click_price or "",
                        })
                except Exception:
                    continue

            # Click back to first available swatch
            if variants:
                try:
                    self.page.evaluate("""() => {
                        const el = document.querySelector('[class*="sku-item--image"]:not([class*="soldOut"])');
                        if (el) el.click();
                    }""")
                except Exception:
                    pass

            return variants if len(variants) > 1 else []
        except Exception as e:
            verbose_output(f"  click variants: {e}")
            return []
    def _parse_variants_from_mtop(self, data: dict) -> List[dict]:
        """Parse mtop API response for variant/SKU data."""
        try:
            # Navigate mtop wrapper
            body = data
            for _ in range(3):
                if isinstance(body, dict) and "data" in body:
                    body = body["data"]
            if isinstance(body, str):
                body = json.loads(body)
            if isinstance(body, dict) and "result" in body:
                body = body["result"]

            # Find SKU property list
            sku_props = (
                body.get("skuPropertyList")
                or body.get("skuProps")
                or body.get("skuList")
                or []
            )
            if not isinstance(sku_props, list) or len(sku_props) < 2:
                return []

            # Separate color and size props
            color_prop = None
            size_prop = None
            for prop in sku_props:
                if not isinstance(prop, dict):
                    continue
                pname = prop.get("propertyName") or prop.get("propName") or ""
                if any(w in pname.lower() for w in ("color", "colour", "색", "컬러")):
                    color_prop = prop
                elif any(w in pname.lower() for w in ("size", "tamanho", "사이즈", "크기")):
                    size_prop = prop

            if not color_prop:
                return []

            # Build variant list from color values
            variants = []
            color_values = color_prop.get("propertyValues") or color_prop.get("values") or []
            for cv in color_values:
                if not isinstance(cv, dict):
                    continue
                cname = cv.get("propertyValueName") or cv.get("name") or cv.get("displayName") or ""
                cimg = cv.get("propertyValueImage") or cv.get("image") or cv.get("skuImage") or ""
                if cimg and cimg.startswith("//"):
                    cimg = "https:" + cimg
                variants.append({
                    "color": cname,
                    "color_image": cimg,
                    "main_image": "",
                    "price": "",
                    "old_price": "",
                })

            # If we also found size prop, enrich
            if size_prop and variants:
                size_values = size_prop.get("propertyValues") or size_prop.get("values") or []
                sizes = []
                for sv in size_values:
                    if isinstance(sv, dict):
                        sn = sv.get("propertyValueName") or sv.get("name") or ""
                        if sn:
                            sizes.append(sn)
                if sizes:
                    for v in variants:
                        v["sizes"] = sizes

            return variants if len(variants) > 1 else []
        except Exception as e:
            verbose_output(f"  parse variants: {e}")
            return []

    def _format_api_variant_price(self, raw) -> str:
        """Return a clean numeric price string from AliExpress API price fields."""
        if not raw:
            return ""
        if isinstance(raw, dict):
            raw = (
                raw.get("value")
                or raw.get("formatedAmount")
                or raw.get("formattedAmount")
                or raw.get("amount")
                or ""
            )
        text = str(raw)
        if "|" in text:
            for part in text.split("|"):
                cleaned = re.sub(r"[^\d.]", "", part)
                if cleaned:
                    return cleaned
        int_part, dec_part = self._parse_price_from_api(text)
        if not int_part or int_part == "0":
            return ""
        return int_part if not dec_part or dec_part == "00" else f"{int_part}.{dec_part}"

    def _parse_sku_variants_from_api_body(self, body: dict) -> List[dict]:
        """Build one variant entry per AliExpress child SKU from SKU + PRICE modules."""
        try:
            sku_mod = body.get("SKU") or body.get("sku") or {}
            price_mod = body.get("PRICE") or body.get("price") or {}
            sku_paths = sku_mod.get("skuPaths") or sku_mod.get("skuPathList") or []
            if isinstance(sku_paths, dict):
                sku_paths = list(sku_paths.values())
            price_map = (
                price_mod.get("skuPriceInfoMap")
                or price_mod.get("skuIdStrPriceInfoMap")
                or price_mod.get("skuPriceList")
                or {}
            )
            if isinstance(price_map, list):
                price_map = {
                    str(item.get("skuIdStr") or item.get("skuId") or ""): item
                    for item in price_map
                    if isinstance(item, dict)
                }
            if not isinstance(sku_paths, list) or not isinstance(price_map, dict):
                return []

            prop_lookup = {}
            color_prop_ids = set()
            size_prop_ids = set()
            props = sku_mod.get("skuProperties") or sku_mod.get("productSKUPropertyList") or []
            for prop in props if isinstance(props, list) else []:
                if not isinstance(prop, dict):
                    continue
                prop_id = str(prop.get("skuPropertyId") or prop.get("propertyId") or prop.get("id") or "")
                prop_name = prop.get("skuPropertyName") or prop.get("propertyName") or prop.get("name") or ""
                lower_name = str(prop_name).lower()
                if any(token in lower_name for token in ("color", "colour", "색상", "색")) or prop.get("hasSkuImage"):
                    color_prop_ids.add(prop_id)
                if any(token in lower_name for token in ("size", "tamanho", "사이즈", "크기")):
                    size_prop_ids.add(prop_id)
                values = prop.get("skuPropertyValues") or prop.get("propertyValues") or prop.get("values") or []
                for val in values if isinstance(values, list) else []:
                    if not isinstance(val, dict):
                        continue
                    value_id = str(
                        val.get("propertyValueIdLong")
                        or val.get("propertyValueId")
                        or val.get("id")
                        or ""
                    )
                    if not prop_id or not value_id:
                        continue
                    prop_lookup[(prop_id, value_id)] = {
                        "prop_name": prop_name,
                        "name": (
                            val.get("propertyValueDefinitionName")
                            or val.get("propertyValueDisplayName")
                            or val.get("propertyValueName")
                            or val.get("name")
                            or ""
                        ),
                        "image": val.get("skuPropertyImagePath") or val.get("propertyValueImage") or "",
                    }

            variants = []
            for path in sku_paths:
                if not isinstance(path, dict):
                    continue
                sku_id = str(path.get("skuIdStr") or path.get("skuId") or "")
                sku_attr = str(path.get("skuAttr") or path.get("path") or "")
                price_info = price_map.get(sku_id) or price_map.get(str(path.get("skuId") or ""))
                if not sku_id or not isinstance(price_info, dict):
                    continue

                color_parts = []
                size_parts = []
                other_parts = []
                color_image = ""
                for segment in re.split(r"[;,\s]+", sku_attr):
                    if ":" not in segment:
                        continue
                    prop_part, value_part = segment.split(":", 1)
                    value_id = value_part.split("#", 1)[0]
                    raw_name = value_part.split("#", 1)[1] if "#" in value_part else ""
                    meta = prop_lookup.get((prop_part, value_id), {})
                    label = meta.get("name") or raw_name or value_id
                    if prop_part in color_prop_ids:
                        color_parts.append(label)
                        color_image = color_image or meta.get("image", "")
                    elif prop_part in size_prop_ids:
                        size_parts.append(label)
                    else:
                        other_parts.append(label)
                        color_image = color_image or meta.get("image", "")

                color = " / ".join(color_parts or other_parts) or sku_id
                size = " / ".join(size_parts) if size_parts else "One Size"
                price = self._format_api_variant_price(
                    price_info.get("salePriceLocal")
                    or price_info.get("salePriceString")
                    or price_info.get("salePrice")
                    or price_info.get("skuActivityAmount")
                    or price_info.get("price")
                )
                old_price = self._format_api_variant_price(
                    price_info.get("originalPrice")
                    or price_info.get("skuAmount")
                    or price_info.get("originalPriceLocal")
                )
                if color_image and color_image.startswith("//"):
                    color_image = "https:" + color_image

                variants.append({
                    "sku_id": sku_id,
                    "color": color,
                    "color_image": color_image,
                    "sizes": [size],
                    "sold_out": int(path.get("skuStock") or 0) <= 0,
                    "stock": path.get("skuStock"),
                    "main_image": "",
                    "price": price,
                    "old_price": old_price,
                })

            return variants
        except Exception as e:
            verbose_output(f"  parse sku price variants: {e}")
            return []

    def _image_key(self, url: str) -> str:
        """Stable image key that ignores AliExpress resize suffixes."""
        url = self._normalize_desc_image_url(url)
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path).lower()
        match = re.match(r"(.+?\.(?:jpg|jpeg|png|webp))", filename, re.I)
        return match.group(1) if match else url.lower().split("?", 1)[0]

    def _remove_variant_images_from_gallery(self, result: dict) -> None:
        """Remove SKU/color swatch images from the main gallery image list."""
        variants = result.get("variants") or []
        variant_urls = []
        for variant in variants:
            if isinstance(variant, dict) and variant.get("color_image"):
                variant_urls.append(variant["color_image"])
        color_images = result.get("color_images") or {}
        if isinstance(color_images, dict):
            variant_urls.extend(color_images.values())
        variant_urls.extend(result.get("variant_images") or [])

        variant_keys = {self._image_key(url) for url in variant_urls if url}
        if not variant_keys:
            return

        clean_images = []
        removed = []
        for url in result.get("images", []) or []:
            if self._image_key(url) in variant_keys:
                removed.append(url)
            else:
                clean_images.append(url)

        if removed:
            result["images"] = clean_images
            existing = result.get("variant_images") or []
            result["variant_images"] = self._dedupe_urls(existing + variant_urls)

    def _normalize_desc_image_url(self, url: str) -> str:
        """Normalize a seller-description image URL."""
        url = (url or "").strip().strip("\"'")
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        return url

    def _dedupe_urls(self, urls: List[str]) -> List[str]:
        result = []
        seen = set()
        for url in urls:
            url = self._normalize_desc_image_url(url)
            key = self._desc_image_key(url)
            if not url or key in seen:
                continue
            seen.add(key)
            result.append(url)
        return result

    def _desc_image_key(self, url: str) -> str:
        """Build a stable key so resized AliExpress copies dedupe to the original image."""
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path).lower()
        match = re.match(r"(.+?\.(?:jpg|jpeg|png|webp))", filename, re.I)
        return match.group(1) if match else url.lower().split("?", 1)[0]

    def _extract_desc_image_urls_from_payload(self, payload) -> List[str]:
        """Extract detail image URLs from JSON payloads or HTML snippets."""
        urls = []
        image_re = re.compile(r"(?:https?:)?//[^'\"()\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^'\"()\s<>]*)?", re.I)

        def walk(obj):
            if isinstance(obj, dict):
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)
            elif isinstance(obj, str):
                for match in image_re.findall(obj):
                    urls.append(match)
                for match in re.findall(r"<img[^>]+(?:src|data-src|data-lazy-src)=['\"]([^'\"]+)['\"]", obj, re.I):
                    urls.append(match)

        walk(payload)
        return self._dedupe_urls(urls)

    def _extract_desc_urls(self) -> List[str]:
        """Find all AliExpress seller-description URLs from intercepted API data."""
        wanted = ("nativeDescUrl", "pcDescUrl", "msiteDescUrl", "descUrl", "descriptionUrl")
        priority = {name: idx for idx, name in enumerate(wanted)}
        found = []

        def walk(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in wanted and isinstance(value, str) and value:
                        found.append((priority[key], value))
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)
            elif isinstance(obj, str):
                try:
                    decoded = json.loads(obj)
                except Exception:
                    return
                walk(decoded)

        for pattern in self._API_PATTERNS:
            data = self._intercepted_data.get(pattern)
            if data:
                walk(data)

        ordered = [url for _, url in sorted(found, key=lambda item: item[0])]
        return self._dedupe_urls(ordered)

    def _fetch_text(self, url: str) -> str:
        """Fetch a description resource, preferring browser context when available."""
        for _ in range(2):
            if self.page:
                try:
                    js_url = json.dumps(url)
                    raw = self.page.evaluate(f"""async () => {{
                        try {{
                            const resp = await fetch({js_url}, {{ credentials: 'include' }});
                            return await resp.text();
                        }} catch(e) {{ return ''; }}
                    }}""")
                    if raw:
                        return raw
                except Exception:
                    pass
            try:
                import requests
                resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                if resp.ok and resp.text:
                    return resp.text
            except Exception:
                pass
        return ""

    def _extract_desc_images_from_page(self) -> List[str]:
        """Fallback: read currently rendered product-description images from DOM."""
        if not self.page:
            return []
        try:
            urls = self.page.evaluate("""() => {
                const roots = [
                    '#product-description',
                    '[class*="detail-desc"]',
                    '[class*="product-description"]',
                    '[class*="description--"]',
                    '[data-pl*="product-description"]'
                ];
                const found = [];
                const add = (value) => { if (value && /\\.(jpg|jpeg|png|webp)(\\?|$)/i.test(value)) found.push(value); };
                for (const selector of roots) {
                    document.querySelectorAll(selector).forEach(root => {
                        root.querySelectorAll('img').forEach(img => {
                            add(img.getAttribute('src'));
                            add(img.getAttribute('data-src'));
                            add(img.getAttribute('data-lazy-src'));
                            const srcset = img.getAttribute('srcset') || '';
                            if (srcset) add(srcset.split(',')[0].trim().split(/\\s+/)[0]);
                        });
                        const html = root.innerHTML || '';
                        [...html.matchAll(/(?:https?:)?\\/\\/[^'"()\\s<>]+?\\.(?:jpg|jpeg|png|webp)(?:\\?[^'"()\\s<>]*)?/ig)]
                            .forEach(match => add(match[0]));
                    });
                }
                return found;
            }""")
            return self._dedupe_urls(urls or [])
        except Exception:
            return []

    def _trigger_description_lazy_load(self) -> None:
        """Nudge the page so AliExpress loads the seller description module."""
        if not self.page:
            return
        try:
            self.page.evaluate("""() => {
                const selectors = [
                    '#product-description',
                    '[class*="detail-desc"]',
                    '[class*="product-description"]',
                    '[class*="description--"]',
                    '[data-pl*="product-description"]'
                ];
                const root = selectors.map(sel => document.querySelector(sel)).find(Boolean);
                if (root) {
                    root.scrollIntoView({ block: 'center' });
                } else {
                    window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.75));
                }
                window.dispatchEvent(new Event('scroll'));
            }""")
        except Exception:
            pass

    def _wait_for_desc_sources(self) -> None:
        """Wait briefly for late description API data or rendered detail images."""
        if not self.page:
            return
        import time
        wait_seconds = getattr(self, "_DESC_WAIT_SECONDS", 8)
        poll_seconds = getattr(self, "_DESC_POLL_SECONDS", 0.5)
        deadline = time.time() + max(0, wait_seconds)

        while True:
            if self._extract_desc_urls() or self._extract_desc_images_from_page():
                return
            if time.time() >= deadline:
                return
            self._trigger_description_lazy_load()
            time.sleep(max(0.01, poll_seconds))

    def _fetch_desc_images(self) -> List[str]:
        """Fetch seller product description images from native/PC/mobile desc resources."""
        images = []
        try:
            self._wait_for_desc_sources()
            for desc_url in self._extract_desc_urls():
                raw = self._fetch_text(desc_url)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = raw
                images.extend(self._extract_desc_image_urls_from_payload(payload))
            if not images:
                images.extend(self._extract_desc_images_from_page())
        except Exception as e:
            verbose_output(f"  desc images: {e}")
        return self._dedupe_urls(images)

    def _extract_from_api(self) -> Optional[dict]:
        """Extract product data: region switch → JS eval → API → merge → fallback HTML."""
        # Switch to correct region for accurate pricing
        self._switch_region()

        js_result = None
        api_result = None

        # Tier 1: JS evaluation (fast, reliable, works even before full load)
        js_result = self._extract_via_js()

        # Tier 2: API interception (richer structured data)
        if self._intercepted_data:
            best = None
            for pattern in self._API_PATTERNS:
                data = self._intercepted_data.get(pattern)
                if data is None:
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    score = self._score_api_response(item)
                    if score > 0 and (best is None or score > best[0]):
                        best = (score, item)
            if best and best[0] >= 2:
                api_result = self._parse_api_data(best[1])

        # Merge: prefer API for prices/images, JS for name/seller
        if js_result and api_result:
            merged = dict(js_result)
            for key in ('current_price_integer', 'current_price_decimal',
                         'old_price_integer', 'old_price_decimal',
                         'discount_percentage', 'images', 'description'):
                val = api_result.get(key)
                if val and val not in ('0', '00', 'N/A', '', [], ['One Size']):
                    merged[key] = val
            if api_result.get('seller_name'):
                merged['seller_name'] = api_result['seller_name']
            if api_result.get('colors'):
                merged['colors'] = api_result['colors']
            if api_result.get('sizes') and api_result['sizes'] != ['One Size']:
                merged['sizes'] = api_result['sizes']
            if api_result.get('variants'):
                merged['variants'] = api_result['variants']
            if api_result.get('color_images'):
                merged['color_images'] = api_result['color_images']
            merged['_source'] = 'js+api'
            result = merged
        else:
            result = js_result or api_result

        # Tier 3: Click through color swatches for accurate color names + sizes per variant
        if result:
            clicked_variants = [] if result.get('variants') else self._extract_variants_click()
            if clicked_variants:
                # Use clicked data (richer: per-color sizes + per-color price)
                # Parse per-variant clicked price; fall back to product-level current price
                def _parse_clicked_price(raw):
                    if not raw:
                        return None
                    import re
                    m = re.search(r'([\\d,\\.]+)', str(raw).replace(',', '.').replace(' ', ''))
                    if m:
                        try:
                            return f"{float(m.group(1)):.2f}"
                        except:
                            pass
                    return None
                proj_cur = f"{result.get('current_price_integer','')}.{result.get('current_price_decimal','00')}"
                proj_cur_f = proj_cur if result.get('current_price_integer') and result['current_price_integer'] != '0' else ''
                variants = []
                for cv in clicked_variants:
                    cv_price_raw = cv.get("price", "")
                    cv_price = _parse_clicked_price(cv_price_raw) or proj_cur_f or ""
                    variants.append({
                        "color": cv.get("color", ""),
                        "color_image": cv.get("color_image", ""),
                        "sizes": cv.get("sizes", result.get("sizes", [])),
                        "sold_out": cv.get("sold_out", False),
                        "main_image": result.get('images', [None])[0] if result.get('images') else '',
                        "price": cv_price,
                    })
                result['variants'] = variants
                # Split by position: last N images = variant images (one per color)
                all_imgs = result.get('images', []) or []
                n = len(variants)
                if n > 0 and len(all_imgs) > n:
                    result['variant_images'] = all_imgs[-n:]
                    result['images'] = all_imgs[:-n]
                    for i, v in enumerate(variants):
                        if i < n:
                            v['color_image'] = result['variant_images'][i]
                # Also update colors/sizes from clicked data for consistency
                result['colors'] = [v['color'] for v in variants]
                all_sizes = []
                for v in variants:
                    for s in v.get('sizes', []):
                        if s not in all_sizes:
                            all_sizes.append(s)
                if all_sizes:
                    result['sizes'] = all_sizes
                verbose_output(
                    f"{BackgroundColors.GREEN}Clicked {len(variants)} color swatches, "
                    f"{len(all_sizes)} sizes total{Style.RESET_ALL}"
                )
            elif not result.get('variants'):
                # Fallback: build from JS-extracted colors (single product-level price)
                colors = result.get('colors', []) or []
                variant_imgs = result.get('variant_images', []) or []
                proj_cur = f"{result.get('current_price_integer','')}.{result.get('current_price_decimal','00')}"
                proj_cur_f = proj_cur if result.get('current_price_integer') and result['current_price_integer'] != '0' else ''
                if colors:
                    variants = []
                    for i, cn in enumerate(colors):
                        ci = result.get('color_images', {}).get(cn, '')
                        if not ci and i < len(variant_imgs):
                            ci = variant_imgs[i]
                        variants.append({
                            "color": cn,
                            "color_image": ci,
                            "sizes": result.get("sizes", []),
                            "main_image": result.get('images', [None])[0] if result.get('images') else '',
                            "price": proj_cur_f,
                        })
                    result['variants'] = variants

        if result:
            self._remove_variant_images_from_gallery(result)

        # Fetch seller description images from API
        if result:
            desc_imgs = self._fetch_desc_images()
            if desc_imgs:
                result['description_images'] = desc_imgs
                verbose_output(
                    f"{BackgroundColors.GREEN}Description images: "
                    f"{BackgroundColors.CYAN}{len(desc_imgs)}{Style.RESET_ALL}"
                )

        return result

    def _score_api_response(self, data: dict) -> int:
        """Score API response by how many product fields it contains."""
        product_keys = {
            "title", "productId", "price", "salePrice", "originalPrice",
            "subject", "productName", "name", "detail", "sku",
            "images", "image", "description", "seller", "store",
            "variants", "skus", "specs", "shipping",
        }
        score = 0

        def _count(obj, depth=0):
            nonlocal score
            if depth > 10 or obj is None:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in product_keys:
                        score += 1
                    if isinstance(v, (dict, list)):
                        _count(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj[:5]:  # Only scan first 5 to avoid infinite
                    _count(item, depth + 1)

        _count(data)
        return score

    def _parse_api_data(self, data: dict) -> Optional[dict]:
        """Parse intercepted API response into standard product_data format."""
        try:
            # Navigate common API response wrappers
            body = data
            # aeglodetailweb wraps in "body" key
            if isinstance(body, dict) and "body" in body and isinstance(body["body"], dict):
                body = body["body"]
            # mtop wraps in "data" → sometimes another "data"
            for _ in range(3):
                if isinstance(body, dict) and "data" in body:
                    body = body["data"]
            if isinstance(body, str):
                body = json.loads(body)
            if isinstance(body, dict) and "result" in body:
                body = body["result"]

            if not isinstance(body, dict):
                return None

            name = (
                body.get("subject")
                or body.get("title")
                or body.get("productName")
                or body.get("name", "")
                or ((body.get("GLOBAL_DATA") or {}).get("globalData") or {}).get("subject", "")
                or (body.get("PRODUCT_TITLE") or {}).get("text", "")
            )
            if not name:
                return None

            # Prices — handle multiple formats
            cur_price_raw = (
                body.get("salePrice")
                or body.get("price")
                or body.get("formatedPrice")
                or ""
            )
            old_price_raw = (
                body.get("originalPrice")
                or body.get("oldPrice")
                or ""
            )
            cur_int, cur_dec = self._parse_price_from_api(cur_price_raw)
            old_int, old_dec = self._parse_price_from_api(old_price_raw)

            # Discount
            discount = body.get("discount") or ""
            if not discount and old_int and old_int != "N/A" and cur_int:
                try:
                    ov = float(f"{old_int}.{old_dec}")
                    cv = float(f"{cur_int}.{cur_dec}")
                    if ov > 0:
                        discount = f"{int(round((ov - cv) / ov * 100))}%"
                except Exception:
                    pass

            # Images
            images = []
            if body.get("imageUrlList"):
                images = body["imageUrlList"]
            elif body.get("imageList"):
                images = body["imageList"]
            elif body.get("images"):
                raw_imgs = body["images"]
                if isinstance(raw_imgs, list):
                    images = [
                        i.get("url") or i.get("imageUrl") or str(i)
                        for i in raw_imgs
                        if isinstance(i, (dict, str))
                    ]
            if isinstance(images, str):
                images = [images]

            # Variants / SKUs
            api_variants = self._parse_sku_variants_from_api_body(body)
            variants = body.get("skuList") or body.get("variants") or body.get("skuAttr") or []
            colors = []
            sizes = []
            sku_map = {}
            color_images = {}
            if api_variants:
                for v in api_variants:
                    color = v.get("color", "")
                    if color and color not in colors:
                        colors.append(color)
                    for size in v.get("sizes", []) or []:
                        if size and size not in sizes:
                            sizes.append(size)
                    if color and v.get("color_image"):
                        color_images[color] = v["color_image"]
                    if color:
                        sku_map[color] = {
                            "price": v.get("price", ""),
                            "image": v.get("color_image", ""),
                            "sku_id": v.get("sku_id", ""),
                        }
            elif isinstance(variants, list):
                for v in variants:
                    if isinstance(v, dict):
                        prop = v.get("skuAttr") or v.get("prop") or v.get("name", "")
                        val = v.get("skuValue") or v.get("value") or v.get("propValue", "")
                        price = v.get("skuPrice") or v.get("price") or ""
                        if "color" in str(prop).lower() or "색" in str(prop):
                            if val and val not in colors:
                                colors.append(val)
                        elif "size" in str(prop).lower() or "사이즈" in str(prop):
                            if val and val not in sizes:
                                sizes.append(val)
                        if val:
                            sku_map[val] = {
                                "price": price,
                                "image": v.get("skuImage") or v.get("img") or "",
                            }

            # Seller info
            seller = body.get("seller") or body.get("store") or body.get("storeInfo") or {}
            if isinstance(seller, dict):
                seller_name = seller.get("name") or seller.get("storeName") or ""
                seller_url = seller.get("url") or seller.get("storeUrl") or ""
            else:
                seller_name = str(seller) if seller else ""
                seller_url = ""

            # Description
            desc = body.get("description") or body.get("detail") or ""

            return {
                "name": name,
                "current_price_integer": cur_int or "0",
                "current_price_decimal": cur_dec or "00",
                "old_price_integer": old_int or "N/A",
                "old_price_decimal": old_dec or "N/A",
                "discount_percentage": str(discount) if discount else "N/A",
                "description": str(desc) if desc else "",
                "url": self.product_url,
                "images": images,
                "colors": colors,
                "sizes": sizes or ["One Size"],
                "variants": api_variants,
                "color_images": color_images,
                "sku_map": sku_map,
                "seller_name": seller_name,
                "seller_url": seller_url,
                "is_international": False,
            }
        except Exception as e:
            verbose_output(
                f"{BackgroundColors.YELLOW}API parse error: {e}{Style.RESET_ALL}"
            )
            return None

    def _parse_price_from_api(self, raw) -> Tuple[str, str]:
        """Parse price string into (integer_part, decimal_part).

        Handles multiple formats correctly:
        - Korean Won: "₩1,560" → ("1560", "00")  (comma = thousands, no decimal)
        - Brazilian Real: "R$ 1.234,56" → ("1234", "56")  (comma = decimal)
        - US Dollar: "$12.99" → ("12", "99")
        - Japanese Yen: "¥2,980" → ("2980", "00")
        """
        if not raw:
            return ("0", "00")
        if isinstance(raw, dict):
            raw = (
                raw.get("formatedAmount")
                or raw.get("value")
                or raw.get("amount")
                or ""
            )
        if not raw:
            return ("0", "00")
        raw_str = str(raw).strip()
        if not raw_str:
            return ("0", "00")

        # Determine currency type from symbols
        is_yen_like = any(c in raw_str for c in '₩¥円')  # KRW, JPY — no decimals
        is_brl_like = 'R$' in raw_str  # BRL — comma is decimal
        is_eur_like = any(c in raw_str for c in '€£')  # EUR, GBP — dot is decimal

        # Strip all non-digit/decimal characters except comma and dot
        cleaned = re.sub(r'[^\d.,]', '', raw_str).strip()
        if not cleaned:
            return ("0", "00")

        # Count separators
        comma_count = cleaned.count(',')
        dot_count = cleaned.count('.')

        if comma_count == 0 and dot_count == 0:
            # Plain integer: "171370"
            return (cleaned, "00")

        if is_yen_like or is_brl_like:
            # KRW/JPY/BRL: comma is thousands, last dot might be decimal
            if comma_count >= 1 and dot_count == 0:
                # "1,560" → 1560 (comma = thousands)
                return (cleaned.replace(',', ''), "00")
            elif dot_count == 1 and comma_count == 0:
                # "1.560" → could be thousands (e.g. 1560) or decimal (1.56)
                # In KRW context, dot is thousands: "1.560" = 1560 won
                if is_yen_like:
                    return (cleaned.replace('.', ''), "00")
                else:
                    # BRL: "1.560" → ambiguous, treat last 3 digits after dot as decimal? No.
                    # Actually BRL uses comma for decimal and dot for thousands
                    return (cleaned.replace('.', ''), "00")
            elif comma_count == 1 and dot_count >= 1:
                # BRL: "1.234,56" → (1234, 56)
                parts = cleaned.split(',')
                int_part = parts[0].replace('.', '')
                dec_part = parts[1][:2].ljust(2, '0')
                return (int_part, dec_part)
        else:
            # USD/EUR style: dot is decimal, comma is thousands
            if dot_count == 1 and comma_count == 0:
                # "12.99" → (12, 99)
                parts = cleaned.split('.')
                if len(parts[1]) <= 2:
                    return (parts[0], parts[1].ljust(2, '0'))
                else:
                    # "1234.567" → unlikely but treat dot as thousands
                    return (cleaned.replace('.', ''), "00")
            elif comma_count == 1 and dot_count == 0:
                # "1,234" → (1234, 00) (comma = thousands)
                if len(cleaned.split(',')[1]) == 2:
                    # "12,34" → could be decimal
                    return (cleaned.replace(',', '.').split('.')[0], cleaned.split(',')[1])
                return (cleaned.replace(',', ''), "00")
            elif dot_count == 0 and comma_count == 0:
                return (cleaned, "00")

        # Fallback: strip all non-digits
        digits_only = re.sub(r'[^\d]', '', cleaned)
        if not digits_only:
            return ("0", "00")
        return (digits_only, "00")

    # ── Product Name ──────────────────────────────────────────────────

    def extract_product_name(self, soup) -> str:
        for tag, attrs in HTML_SELECTORS["product_name"]:
            name_element = soup.find(tag, attrs if attrs else None)
            if name_element:
                raw = name_element.get_text(separator=" ", strip=True)
                product_name = normalize_product_name(raw_name=raw)
                if product_name:
                    verbose_output(
                        f"{BackgroundColors.GREEN}Product name: "
                        f"{BackgroundColors.CYAN}{product_name}{Style.RESET_ALL}"
                    )
                    return product_name
        verbose_output(
            f"{BackgroundColors.YELLOW}Product name not found.{Style.RESET_ALL}"
        )
        return "Unknown Product"

    # ── International Detection ───────────────────────────────────────

    def detect_international(self, soup) -> bool:
        """Detect via import declaration text or Country of Origin field."""
        verbose_output(
            f"{BackgroundColors.GREEN}Checking if international...{Style.RESET_ALL}"
        )
        try:
            international_elements = soup.find_all("span", class_="NzLZHV")
            for el in international_elements:
                if not isinstance(el, Tag):
                    continue
                text = el.get_text(strip=True)
                if "Produto International objeto de declaração de importação" in text:
                    verbose_output(
                        f"{BackgroundColors.YELLOW}International (import declaration).{Style.RESET_ALL}"
                    )
                    return True

            detail_labels = soup.find_all("h3", HTML_SELECTORS["detail_label"])
            for label in detail_labels:
                if not isinstance(label, Tag):
                    continue
                label_text = label.get_text(strip=True)
                if "País de Origem" in label_text or "Country of Origin" in label_text:
                    parent = label.parent
                    if parent and isinstance(parent, Tag):
                        value_el = parent.find("div")
                        if value_el and isinstance(value_el, Tag):
                            country = value_el.get_text(strip=True)
                            verbose_output(
                                f"{BackgroundColors.GREEN}Country of Origin: "
                                f"{BackgroundColors.CYAN}{country}{Style.RESET_ALL}"
                            )
                            if country.lower() not in ["brasil", "brazil"]:
                                verbose_output(
                                    f"{BackgroundColors.YELLOW}International (from {country}).{Style.RESET_ALL}"
                                )
                                return True
                            else:
                                verbose_output(
                                    f"{BackgroundColors.GREEN}Domestic (Brazil).{Style.RESET_ALL}"
                                )
                                return False

            verbose_output(
                f"{BackgroundColors.YELLOW}International indicators not found, assuming domestic.{Style.RESET_ALL}"
            )
            return False
        except Exception as e:
            print(
                f"{BackgroundColors.YELLOW}Warning: detect international error: {e}{Style.RESET_ALL}"
            )
            return False

    def prefix_international_name(self, product_name: str) -> str:
        """Override: AliExpress uses NBSP normalization."""
        if not product_name.upper().startswith("INTERNATIONAL"):
            product_name = f"International - {product_name}"
            product_name = product_name.replace(" ", " ")
            product_name = re.sub(r"\s+", " ", product_name).strip()
            verbose_output(
                f"{BackgroundColors.GREEN}Updated name: "
                f"{BackgroundColors.CYAN}{product_name}{Style.RESET_ALL}"
            )
        return product_name

    def _on_international_detected(self, product_name: str):
        verbose_output(
            f"{BackgroundColors.YELLOW}International prefix added.{Style.RESET_ALL}"
        )

    # ── Prices ───────────────────────────────────────────────────────

    def extract_current_price(self, soup) -> Tuple[str, str]:
        for tag, attrs in HTML_SELECTORS["current_price"]:
            price_el = soup.find(tag, attrs if attrs else None)
            if price_el:
                price_text = price_el.get_text(strip=True)
                match = re.search(
                    r"(\d+(?:[\.,]\d{3})*)[,\.](\d{2})", price_text
                )
                if match:
                    integer = match.group(1).replace(".", "").replace(",", "")
                    decimal = match.group(2)
                    verbose_output(
                        f"{BackgroundColors.GREEN}Current price: R${integer},{decimal}{Style.RESET_ALL}"
                    )
                    return integer, decimal
        verbose_output(
            f"{BackgroundColors.YELLOW}Current price not found.{Style.RESET_ALL}"
        )
        return "0", "00"

    def extract_old_price(
        self,
        soup,
        current_price_int: str = "0",
        current_price_dec: str = "00",
        discount_percentage: str = "N/A",
    ) -> Tuple[str, str]:
        for tag, attrs in HTML_SELECTORS["old_price"]:
            price_el = soup.find(tag, attrs if attrs else None)
            if price_el:
                price_text = price_el.get_text(strip=True)
                match = re.search(
                    r"(\d+(?:[\.,]\d{3})*)[,\.](\d{2})", price_text
                )
                if match:
                    integer = match.group(1).replace(".", "").replace(",", "")
                    decimal = match.group(2)
                    verbose_output(
                        f"{BackgroundColors.GREEN}Old price: R${integer},{decimal}{Style.RESET_ALL}"
                    )
                    return integer, decimal

        # Computational fallback
        if (
            current_price_int not in ("0", "N/A")
            and discount_percentage not in ("N/A", "")
        ):
            try:
                discount_match = re.search(r"(\d+)%", discount_percentage)
                if discount_match:
                    discount_val = float(discount_match.group(1)) / 100.0
                    cur_val = float(f"{current_price_int}.{current_price_dec}")
                    if discount_val < 1.0:
                        orig = round(cur_val / (1.0 - discount_val), 2)
                        integer = str(int(orig))
                        decimal = str(int((orig % 1) * 100)).zfill(2)
                        verbose_output(
                            f"{BackgroundColors.GREEN}Old price calculated: "
                            f"R${integer},{decimal}{Style.RESET_ALL}"
                        )
                        return integer, decimal
            except (ValueError, ZeroDivisionError) as e:
                verbose_output(
                    f"{BackgroundColors.YELLOW}Old price calculation error: {e}{Style.RESET_ALL}"
                )

        verbose_output(f"{BackgroundColors.YELLOW}Old price not found.{Style.RESET_ALL}")
        return "N/A", "N/A"

    def extract_discount_percentage(self, soup) -> str:
        for tag, attrs in HTML_SELECTORS["discount"]:
            discount_el = soup.find(tag, attrs if attrs else None)
            if discount_el:
                discount_text = discount_el.get_text(strip=True)
                match = re.search(r"(\d+%)", discount_text)
                if match:
                    verbose_output(
                        f"{BackgroundColors.GREEN}Discount: {match.group(1)}{Style.RESET_ALL}"
                    )
                    return match.group(1)

        # Compute from prices
        try:
            old_int, old_dec = self.extract_old_price(soup)
            cur_int, cur_dec = self.extract_current_price(soup)
            if old_int and old_int != "N/A" and cur_int:
                old_val = float(f"{old_int}.{old_dec}")
                cur_val = float(f"{cur_int}.{cur_dec}")
                if old_val > 0:
                    discount = int(round(((old_val - cur_val) / old_val) * 100.0))
                    verbose_output(
                        f"{BackgroundColors.GREEN}Computed discount: {discount}%{Style.RESET_ALL}"
                    )
                    return f"{discount}%"
        except Exception:
            pass
        return "N/A"

    # ── Description ─────────────────────────────────────────────────

    def extract_product_description(self, soup) -> str:
        for tag, attrs in HTML_SELECTORS["description"]:
            desc_el = soup.find(tag, attrs if attrs else None)
            if desc_el and isinstance(desc_el, Tag):
                texts = []
                for child in desc_el.find_all(
                    ["p", "span", "h1", "h2", "h3", "li", "div"]
                ):
                    if isinstance(child, Tag):
                        piece = child.get_text(separator=" ", strip=True)
                        if piece:
                            texts.append(piece)
                description = "\n".join(texts).strip()
                description = self.to_sentence_case(description)
                if description and len(description) > 10:
                    verbose_output(
                        f"{BackgroundColors.GREEN}Description: {len(description)} chars.{Style.RESET_ALL}"
                    )
                    return description
        return "No description available"

    # ── Specifications (AliExpress only) ─────────────────────────────

    def extract_specifications(self, soup) -> Dict[str, str]:
        """Extract specification table rows as key-value dict."""
        specs: Dict[str, str] = {}
        try:
            container = soup.find("div", HTML_SELECTORS.get("specs_container"))
            if container and isinstance(container, Tag):
                rows = container.find_all("div", HTML_SELECTORS.get("specs_row"))
                for row in rows:
                    if not isinstance(row, Tag):
                        continue
                    title_el = row.find("div", HTML_SELECTORS.get("specs_title"))
                    value_el = row.find("div", HTML_SELECTORS.get("specs_value"))
                    if (
                        title_el
                        and value_el
                        and isinstance(title_el, Tag)
                        and isinstance(value_el, Tag)
                    ):
                        key = title_el.get_text(strip=True)
                        val = value_el.get_text(strip=True)
                        if key:
                            specs[key] = val
        except Exception as e:
            verbose_output(
                f"{BackgroundColors.YELLOW}Spec extraction warning: {e}{Style.RESET_ALL}"
            )
        return specs

    # ── Images ──────────────────────────────────────────────────────

    def find_image_urls(self, soup) -> List[str]:
        """Find image URLs in product gallery. Upgrades to 960x960."""
        image_urls: List[str] = []
        seen: set = set()

        verbose_output(
            f"{BackgroundColors.GREEN}Extracting image URLs from gallery...{Style.RESET_ALL}"
        )
        try:
            gallery = soup.find("div", HTML_SELECTORS.get("gallery"))
            if gallery and isinstance(gallery, Tag):
                imgs = gallery.find_all("img")
                verbose_output(
                    f"{BackgroundColors.GREEN}Gallery images found: {len(imgs)}{Style.RESET_ALL}"
                )
                for img in imgs:
                    if not isinstance(img, Tag):
                        continue
                    src = (
                        img.get("src")
                        or img.get("data-src")
                        or img.get("data-original")
                    )
                    if not src or not isinstance(src, str):
                        continue
                    # Upgrade resolution
                    src_high = re.sub(
                        r"_\d{2,4}x\d{2,4}(q\d+)?(\.jpg|\.png|\.avif)?",
                        "_960x960q75.jpg",
                        src,
                    )
                    final = src_high if src_high else src
                    if final.startswith("//"):
                        final = "https:" + final
                    if final not in seen and "placeholder" not in final.lower():
                        image_urls.append(final)
                        seen.add(final)

            # Review images
            review_ct = soup.find(
                "div", HTML_SELECTORS.get("review_images_container")
            )
            if review_ct and isinstance(review_ct, Tag):
                for img in review_ct.find_all("img"):
                    if not isinstance(img, Tag):
                        continue
                    src = img.get("src") or img.get("data-src")
                    if (
                        src
                        and isinstance(src, str)
                        and src.endswith(
                            (".jpg_.avif", ".png_.avif", ".jpg", ".png")
                        )
                    ):
                        if src.startswith("//"):
                            src = "https:" + src
                        if src not in seen:
                            image_urls.append(src)
                            seen.add(src)
        except Exception as e:
            print(f"{BackgroundColors.RED}Image extraction error: {e}{Style.RESET_ALL}")

        verbose_output(
            f"{BackgroundColors.GREEN}Found {len(image_urls)} images.{Style.RESET_ALL}"
        )
        return image_urls

    # ── Videos ──────────────────────────────────────────────────────

    def find_video_urls(self, soup) -> List[str]:
        """Find video URLs: <video> tags, <source> tags, and inline links."""
        video_urls: List[str] = []
        seen: set = set()

        verbose_output(
            f"{BackgroundColors.GREEN}Extracting video URLs...{Style.RESET_ALL}"
        )
        try:
            video_tags = soup.find_all("video")
            verbose_output(
                f"{BackgroundColors.GREEN}Video elements: {len(video_tags)}{Style.RESET_ALL}"
            )
            for video in video_tags:
                if not isinstance(video, Tag):
                    continue
                video_url = video.get("src") or video.get("data-src")
                if (
                    video_url
                    and isinstance(video_url, str)
                    and (
                        video_url.endswith(".mp4")
                        or video_url.endswith(".webm")
                        or "m3u8" in video_url
                    )
                ):
                    if video_url not in seen:
                        video_urls.append(video_url)
                        seen.add(video_url)

                for source in video.find_all("source"):
                    if not isinstance(source, Tag):
                        continue
                    src = source.get("src") or source.get("data-src")
                    if (
                        src
                        and isinstance(src, str)
                        and (
                            src.endswith(".mp4")
                            or src.endswith(".webm")
                            or "m3u8" in src
                        )
                    ):
                        if src not in seen:
                            video_urls.append(src)
                            seen.add(src)

            # Inline video links
            for ext in (".mp4", ".webm", ".m3u8"):
                for tag in soup.find_all(string=re.compile(re.escape(ext))):
                    try:
                        text = str(tag)
                        m = re.search(r"https?://[\w\-./%?=,&]+\"?", text)
                        if m:
                            url = m.group(0).strip('"')
                            if url not in seen:
                                video_urls.append(url)
                                seen.add(url)
                    except Exception:
                        pass
        except Exception as e:
            print(f"{BackgroundColors.RED}Video extraction error: {e}{Style.RESET_ALL}")

        verbose_output(
            f"{BackgroundColors.GREEN}Found {len(video_urls)} videos.{Style.RESET_ALL}"
        )
        return video_urls


# =============================================================================
# Standalone Entry Point
# =============================================================================

def main():
    from base_scraper import (
        BackgroundColors,
        output_result,
        calculate_execution_time,
        play_sound,
        RUN_FUNCTIONS,
    )
    import atexit
    from Logger import Logger

    # Setup logger for standalone mode
    logger = Logger(f"./Logs/{Path(__file__).stem}.log", clean=True)
    sys.stdout = logger
    sys.stderr = logger

    print(
        f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
        f"Welcome to AliExpress Scraper!{Style.RESET_ALL}"
    )
    start = datetime.datetime.now()
    test_url = "https://pt.aliexpress.com/item/1005008724358345.html"

    verbose_output(
        f"{BackgroundColors.GREEN}Test URL: {BackgroundColors.CYAN}{test_url}{Style.RESET_ALL}\n"
    )
    try:
        scraper = AliExpress(test_url)
        result = scraper.scrape()
        output_result(result, "AliExpress")
    except Exception as e:
        print(f"{BackgroundColors.RED}Error: {e}{Style.RESET_ALL}")

    finish = datetime.datetime.now()
    print(
        f"{BackgroundColors.GREEN}Start: {start.strftime('%d/%m/%Y - %H:%M:%S')}\n"
        f"{BackgroundColors.GREEN}Finish: {finish.strftime('%d/%m/%Y - %H:%M:%S')}\n"
        f"{BackgroundColors.GREEN}Time: {calculate_execution_time(start, finish)}{Style.RESET_ALL}"
    )
    print(f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}Done.{Style.RESET_ALL}")

    (atexit.register(play_sound) if RUN_FUNCTIONS["Play Sound"] else None)


if __name__ == "__main__":
    main()
