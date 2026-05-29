# Author: Administrator
# Created: 2026-05-27
"""Lightweight Shein product data extractor using page.evaluate().

Replaces BeautifulSoup-based HTML parsing with targeted JavaScript DOM
extraction.  Never pulls the full 3.8 MB HTML into Python — only ~5 KB
of structured JSON per product.
"""

import json, re
from typing import List, Dict, Optional, Any

# ---- Internal JS extractors (run in Chrome/V8, not Python) ----

_JS_EXTRACT_ALL = """() => {
    const result = {};

    // 1. goodsDetailSchema JSON-LD (colors, sizes, prices, images)
    let schemaEl = document.querySelector('script#goodsDetailSchema');
    result.schema = schemaEl ? schemaEl.textContent : '';

    // 2. SKU
    result.sku = '';
    let skuEl = document.querySelector('.product-intro__head-sku-text');
    if (!skuEl) {
        let spans = document.querySelectorAll('span');
        for (let sp of spans) {
            if (/SKU:/i.test(sp.textContent)) { skuEl = sp; break; }
        }
    }
    if (skuEl) {
        let m = skuEl.textContent.match(/SKU:\\s*([\\w\\d]+)/);
        if (m) result.sku = m[1];
    }

    // 3. Product name — document.title first (most reliable), then h1
    result.name = (document.title || '').trim();
    // Strip trailing site markers: "| SHEIN Korea", "- SHEIN", "| SHEIN"
    result.name = result.name.replace(/\\s*[|\\-]\\s*SHEIN\\s*(KOREA|Korea|KR)?\\s*$/i, '').trim();
    // If title is too short or was stripped to nothing, try h1
    if (!result.name || result.name.length < 4) {
        let nameEl = document.querySelector('h1') ||
                     document.querySelector('.fsp-element') ||
                     document.querySelector('[class*="product-name"]');
        if (nameEl) result.name = (nameEl.textContent || '').trim();
    }
    if (!result.name || result.name.length < 4) {
        // Try og:title, meta title
        let mt = document.querySelector('meta[property="og:title"]');
        if (mt) { let c = mt.getAttribute('content'); if (c && c.length > 3) result.name = c.trim(); }
    }
    if (!result.name) result.name = (document.title || '').trim();

    // 4. Current price
    result.currentPrice = '';
    let cpEl = document.querySelector('#productMainPriceId') ||
               document.querySelector('.productPrice__main');
    if (cpEl) result.currentPrice = (cpEl.textContent || '').trim();

    // 5. Old price
    result.oldPrice = '';
    let opEl = document.querySelector('.productEstimatedTagNewRetail__retail') ||
               document.querySelector('.productDiscountInfo__retail');
    if (opEl) result.oldPrice = (opEl.textContent || '').trim();

    // 6. Discount percentage
    result.discount = '';
    let discEl = document.querySelector('.productEstimatedTagNew__percent') ||
                 document.querySelector('.productDiscountPercent');
    if (discEl) result.discount = (discEl.textContent || '').trim();

    // 7. Description — try meta description, og:description, then DOM selectors
    result.description = '';
    let metaDesc = document.querySelector('meta[name="description"]') ||
                   document.querySelector('meta[property="og:description"]');
    if (metaDesc) result.description = (metaDesc.getAttribute('content') || '').trim();
    if (!result.description) {
        let descEl = document.querySelector('.product-intro__attr-list-text') ||
                     document.querySelector('.product-intro__attr-des') ||
                     document.querySelector('.product-intro__attr-wrap') ||
                     document.querySelector('[class*="product-intro__desc"]') ||
                     document.querySelector('[class*="product-detail"]') ||
                     document.querySelector('[class*="goods-desc"]') ||
                     document.querySelector('#goods-detail-content') ||
                     document.querySelector('[class*="description"]');
        if (descEl) result.description = (descEl.innerText || descEl.textContent || '').trim();
    }

    // 8. Shein category from meta
    result.sheinCat = '';
    let metas = document.querySelectorAll('meta');
    for (let m of metas) {
        let nm = (m.getAttribute('name') || '') + (m.getAttribute('property') || '');
        if (nm.toLowerCase().includes('category')) {
            result.sheinCat = m.getAttribute('content') || '';
            break;
        }
    }

    // 9. Color swatch data — search <script> tags for extra color variants
    result.colorSwatches = [];
    let scripts = document.querySelectorAll('script:not([src])');
    for (let s of scripts) {
        let txt = s.textContent || '';
        if (!txt.includes('attr_name')) continue;
        let matches = txt.match(/"attr_name":"색","attr_value_id":"(\\d+)","attr_value":"([^"]+)"[^}]*?"goods_id":"(\\d+)"[^}]*?"goods_color_image":"([^"]+)"[^}]*?"goods_image":"([^"]+)"/g);
        if (!matches) {
            matches = txt.match(/"attr_value":"([^"]+)","goods_id":"(\\d+)"[^}]*?"goods_color_image":"([^"]+)"[^}]*?"goods_image":"([^"]+)"/g);
        }
        if (matches) {
            for (let m of matches) {
                let vals = m.match(/"([^"]+)"/g);
                if (vals && vals.length >= 4) {
                    result.colorSwatches.push(vals.map(v => v.replace(/"/g, '')));
                }
            }
            break;  // found swatches, stop searching
        }
    }

    // 10. skcImages — per-color images from inline script
    result.skcImgs = [];
    for (let s of scripts) {
        let txt = s.textContent || '';
        let si = txt.match(/"skcImages"\\s*:\\s*\\[(.*?)\\]/);
        if (si) {
            let ri = si[1].match(/"([^"]*ltwebstatic[^"]*)"/g);
            if (ri) result.skcImgs = ri.map(u => u.replace(/"/g, ''));
            break;
        }
    }

    return JSON.stringify(result);
}"""

# ---- Python helpers for post-processing JS results ----

def _normalize_price(raw: str) -> str:
    """Extract numeric price from raw text like 'R$ 12,90' or '₩12,900'."""
    if not raw:
        return ""
    # Remove currency symbols, spaces, non-numeric chars except . ,
    cleaned = re.sub(r'[^\\d.,]', '', raw)
    # If comma is decimal separator: "12,90" → "12.90"
    if ',' in cleaned and '.' not in cleaned:
        cleaned = cleaned.replace(',', '.')
    elif ',' in cleaned:
        # "1.234,56" → "1234.56"
        parts = cleaned.split(',')
        if len(parts) == 2 and len(parts[1]) == 2:
            cleaned = parts[0].replace('.', '') + '.' + parts[1]
    return cleaned


def _extract_sizes(jsonld: dict) -> List[str]:
    """Extract size list from goodsDetailSchema hasVariant data."""
    sizes = []
    for v in jsonld.get('hasVariant', []):
        n = v.get('name', '')
        sz = re.search(r'(?:Size|사이즈)\\s+([\\w\\d]+)', n, re.I)
        if not sz:
            sz = re.search(r'(?:Size\\s+)?(EUR\\d+|US\\d+|UK\\d+)', n, re.I)
        s2 = sz.group(1) if sz else ''
        if s2 and s2 not in sizes:
            sizes.append(s2)
    _size_blacklist = ['차트', '차드', '표를', '표', '사이즈', '가이드', '크기', '선택', '선택하세요']
    sizes = [s for s in sizes if s not in _size_blacklist]
    try:
        sizes.sort(key=lambda s: (
            re.match(r'([A-Z]*)(\\d+)', s) and (
                re.match(r'([A-Z]*)(\\d+)', s).group(1),
                int(re.match(r'([A-Z]*)(\\d+)', s).group(2))
            )
        ) or (s, 0))
    except:
        pass
    if not sizes:
        sizes = ["One Size"]
    return sizes


# ---- Main public API ----

def precheck_sku(page) -> Optional[str]:
    """Quickly extract SKU from page to check for duplicates. Returns SKU string or None."""
    try:
        sku = page.evaluate("""() => {
            let el = document.querySelector('.product-intro__head-sku-text');
            if (!el) {
                let spans = document.querySelectorAll('span');
                for (let sp of spans) { if (/SKU:/i.test(sp.textContent)) { el = sp; break; } }
            }
            if (el) {
                let m = el.textContent.match(/SKU:\\s*([\\w\\d]+)/);
                return m ? m[1] : '';
            }
            return '';
        }""")
        return sku if sku else None
    except Exception:
        return None


def extract_name_and_desc(page) -> tuple:
    """Fallback: use BeautifulSoup on full HTML just for name + description.
    HTML is immediately freed after extraction — ~1s memory spike only."""
    from bs4 import BeautifulSoup
    import gc
    try:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        del html
        name = ""
        for tag, attrs in [("span", {"class": "fsp-element"}), ("h1", {"class": "fsp-element"}),
                           ("h1", {}), ("title", {})]:
            el = soup.find(tag, attrs if attrs else None)
            if el:
                txt = el.get_text(strip=True) if tag != "title" else (el.string or "").strip()
                if txt and len(txt) > 3:
                    name = txt
                    break
        if not name and soup.title:
            name = soup.title.string or ""
            name = name.strip()
        desc = ""
        for sel in ["div.product-intro__attr-list-text", "div.product-intro__attr-des",
                     "div.product-intro__attr-wrap", "div.product-intro__desc",
                     "div.product-detail", "#goods-detail-content"]:
            el = soup.select_one(sel)
            if el:
                desc = el.get_text(strip=True)
                if desc: break
        del soup
        gc.collect()
        return name, desc
    except Exception:
        return "", ""


def extract(page) -> List[Dict[str, Any]]:
    """Extract all product data from a loaded Shein product page.

    Args:
        page: Playwright Page object (already navigated to a Shein product).

    Returns:
        List of product dicts (one per color variant), ready for self.products.
    """
    raw = page.evaluate(_JS_EXTRACT_ALL)
    jsd = json.loads(raw)

    # Parse goodsDetailSchema
    schema = {}
    try:
        schema = json.loads(jsd.get('schema', '') or '{}')
    except:
        pass
    # Process skcImages
    skc_imgs = []
    for u in jsd.get('skcImgs', []) or []:
        u = 'https:' + u if u.startswith('//') else u
        u = re.sub(r'_thumbnail_\\d+x\\d+', '_thumbnail_900x', u)
        skc_imgs.append(u)

    # Process color entries from schema
    color_entries = []
    vi = []  # variant images
    prod_sku = jsd.get('sku', '')
    items = schema if isinstance(schema, list) else ([schema] if isinstance(schema, dict) and schema else [])

    _size_bl = ['차트', '차드', '표를', '표', '사이즈', '가이드', '크기', '선택', '선택하세요']
    for it in items:
        if not isinstance(it, dict) or 'hasVariant' not in it:
            continue
        cn = it.get('color', '') or 'Default'
        sizes = []
        szpr = {}

        for v in it.get('hasVariant', []):
            n = v.get('name', '')
            sz = re.search(r'(?:Size|사이즈)\\s+([\\w\\d]+)', n, re.I)
            if not sz:
                sz = re.search(r'(?:Size\\s+)?(EUR\\d+|US\\d+|UK\\d+)', n, re.I)
            s2 = sz.group(1) if sz else ''
            if s2:
                if s2 not in sizes:
                    sizes.append(s2)
                o = v.get('offers', {})
                vp = o.get('price', '') if isinstance(o, dict) else ''
                if vp:
                    szpr[s2] = vp
                v2 = v.get('image', [])
                u = (
                    re.sub(r'_thumbnail_\\d+x\\d+', '_thumbnail_900x', v2[0])
                    if isinstance(v2, list) and v2 else
                    re.sub(r'_thumbnail_\\d+x\\d+', '_thumbnail_900x', v2)
                    if isinstance(v2, str) and v2 else ''
                )
                if u:
                    vi.append(u)

        # Filter & sort (same logic as _extract_sizes)
        sizes = [s for s in sizes if s not in _size_bl]
        try:
            sizes.sort(key=lambda s: (
                re.match(r'([A-Z]*)(\\d+)', s) and (
                    re.match(r'([A-Z]*)(\\d+)', s).group(1),
                    int(re.match(r'([A-Z]*)(\\d+)', s).group(2))
                )
            ) or (s, 0))
        except: pass
        if not sizes: sizes = ["One Size"]

        # Merge skcImages + variant images, dedup
        seen = set()
        ui = []
        for u in skc_imgs + vi:
            if not u.startswith('http'):
                continue
            fn = u.split('/')[-1].split('?')[0].lower()
            if fn.startswith('visa') or fn.startswith('mastercard'):
                continue
            stem = re.sub(r'_thumbnail_\\d+x\\d+', '', fn)
            stem = re.sub(r'\\.(jpg|jpeg|png|webp|gif)$', '', stem)
            if stem not in seen:
                seen.add(stem)
                ui.append(u)

        pr = szpr.get(sizes[0], '') if sizes and szpr else ''
        if not pr:
            for v in it.get('hasVariant', [])[:1]:
                o = v.get('offers', {})
                if isinstance(o, dict) and o.get('price'):
                    pr = o.get('price', '')
        if not pr:
            pr = _normalize_price(jsd.get('currentPrice', ''))

        ci_img = vi[0] if vi else (skc_imgs[0] if skc_imgs else '')
        color_entries.append({
            "color": cn, "sizes": sizes, "images": ui, "price": pr,
            "size_prices": szpr, "color_image": ci_img, "sku": prod_sku,
        })

    if not color_entries:
        color_entries = [{
            "color": "Default", "sizes": ["One Size"], "images": [],
            "price": "", "size_prices": {}, "color_image": "", "sku": "",
        }]

    # Product name & description (priority: DOM > schema > fallback)
    _raw_schema = {}
    try:
        _tmp = json.loads(jsd.get('schema', '') or '{}')
        _first = _tmp[0] if isinstance(_tmp, list) and _tmp else _tmp
        _raw_schema = _first if isinstance(_first, dict) else {}
    except: pass
    name = jsd.get('name', '') or ''
    if not name or name == 'Unknown Product':
        name = _raw_schema.get('name', '') or ''
    if not name:
        name = 'Unknown Product'
    description = jsd.get('description', '') or ''
    if not description:
        description = _raw_schema.get('description', '') or ''
    discount = jsd.get('discount', '') or ''
    shein_cat = jsd.get('sheinCat', '')

    # Current price for fallback
    cur_price = _normalize_price(jsd.get('currentPrice', ''))

    return {
        "name": name,
        "description": description,
        "discount": discount,
        "colors": color_entries,
        "shein_category": shein_cat,
        "swatches": jsd.get('colorSwatches', []) or [],
        "current_price": cur_price,
    }
