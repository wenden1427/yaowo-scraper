"""Extract Shein product links from store/search page."""
import json, time, os, sys
from patchright.sync_api import sync_playwright
def log(msg): print(msg, file=sys.stderr)

url = sys.argv[1] if len(sys.argv) > 1 else ""
if not url:
    print(json.dumps({"error": "No URL"}))
    sys.exit(1)

try:
    import subprocess as sp
    sp.run("taskkill /F /IM chrome.exe 2>nul", shell=True)
    sp.run("taskkill /F /IM chromium.exe 2>nul", shell=True)
    time.sleep(3)

    _fp_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    _fp_profile = os.path.join(_fp_root, 'data', 'chrome_profile')
    _fp_cloak = os.path.join(_fp_root, 'cloakbrowser', 'chrome.exe')
    if not os.path.exists(_fp_cloak):
        _fp_cloak = ''
    os.makedirs(_fp_profile, exist_ok=True)

    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=_fp_profile,
        executable_path=_fp_cloak if _fp_cloak else None,
        headless=False,
        args=["--no-sandbox"],
        ignore_default_args=["--enable-automation","--enable-unsafe-swiftshader"],
        viewport={"width": 1920, "height": 1080})
    page = ctx.new_page()
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(3)

    # CAPTCHA detection
    captcha_wait = 0
    while captcha_wait < 120:
        cur_url = page.url
        if "challenge" not in cur_url and "risk" not in cur_url:
            break
        if captcha_wait == 0:
            log("[CAPTCHA] Please solve...")
        if captcha_wait % 30 == 0 and captcha_wait > 0:
            log(f"  Still waiting... ({captcha_wait}s)")
        time.sleep(2)
        captcha_wait += 2

    # Auto-scroll to load all lazy products
    prev_count = 0
    stable_count = 0
    for i in range(80):
        try:
            page.evaluate("""
                window.scrollTo(0, document.body.scrollHeight);
                document.querySelectorAll('*').forEach(el => {
                    if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
                });
            """)
        except:
            pass
        time.sleep(0.8)
        try:
            cur = page.evaluate("document.querySelectorAll('a[href*=\"-p-\"]').length")
        except:
            cur = prev_count
        try:
            for sel in ["button:has-text('Show more')", "button:has-text('Load more')",
                        "button:has-text('더 보기')", "button:has-text('查看更多')",
                        "[data-testid='load-more']", ".load-more", ".show-more"]:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    cur = page.evaluate("document.querySelectorAll('a[href*=\"-p-\"]').length")
        except:
            pass
        if cur == prev_count:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
        prev_count = cur

    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)

    # Extract links
    extract_js = """
        Array.from(document.querySelectorAll('a[href*="-p-"]'))
            .map(a => a.href)
            .filter(h => /-p-\\d+\\.html/.test(h))
            .filter((h, i, a) => a.indexOf(h) === i)
    """
    links = page.evaluate(extract_js)

    page.close(); ctx.close(); pw.stop()

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted_links.txt")
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(links))
    print(json.dumps({"count": len(links), "file": out_file}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
