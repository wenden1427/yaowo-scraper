"""
================================================================================
E-Commerce Base Scraper — base_scraper.py
================================================================================
Platform-agnostic infrastructure shared by AliExpress, Shein, Shopee, and
MercadoLivre scrapers.

Extracted from AliExpress.py and Shein.py to eliminate ~1500 lines of
duplicated browser lifecycle, page loading, scrolling, media download, and
file output code.

Subclasses only need to provide:
    - PLATFORM_NAME (str)
    - HTML_SELECTORS (dict)
    - extract_product_name(soup) -> str
    - extract_current_price(soup) -> (int_part, dec_part)
    - extract_old_price(soup, ...) -> (int_part, dec_part)
    - extract_discount_percentage(soup) -> str
    - extract_product_description(soup) -> str | dict
    - find_image_urls(soup) -> list[str]
    - find_video_urls(soup) -> list[str]
    - detect_international(soup) -> bool
    - scrape_product_info(html_content) -> dict  (or use default)
"""

import atexit
import datetime
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
from bs4 import BeautifulSoup, Tag
from colorama import Style
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from product_utils import normalize_product_name
from typing import Optional, Any, List, Tuple
from urllib.parse import urljoin, urlparse

import requests


# =============================================================================
# Module Constants
# =============================================================================

VERBOSE = False

# Output
OUTPUT_DIRECTORY = "./Outputs/"

# Browser — auto-detect cloakbrowser + profile (same as GUI)
_SCRAPER_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
_CLOAK_EXE = os.path.join(_SCRAPER_ROOT, "cloakbrowser", "chrome.exe")
_DATA_PROFILE = os.path.join(_SCRAPER_ROOT, "data", "chrome_profile")

CHROME_PROFILE_PATH = os.getenv("CHROME_PROFILE_PATH", "")
CHROME_EXECUTABLE_PATH = os.getenv("CHROME_EXECUTABLE_PATH", "")
# Auto-detect: prefer cloakbrowser if available
if not CHROME_EXECUTABLE_PATH and os.path.exists(_CLOAK_EXE):
    CHROME_EXECUTABLE_PATH = _CLOAK_EXE
# Auto-detect data profile if no env override
if not CHROME_PROFILE_PATH:
    os.makedirs(_DATA_PROFILE, exist_ok=True)
    CHROME_PROFILE_PATH = _DATA_PROFILE

# Proxy (residential static IP for anti-detection)
PROXY_SERVER = os.getenv("PROXY_SERVER", "")      # e.g. "http://user:pass@host:port"
HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
PAGE_LOAD_TIMEOUT = 90000
NETWORK_IDLE_TIMEOUT = 10000
SCROLL_PAUSE_TIME = 0.5
SCROLL_STEP = 300

# Product description template (subclasses override PLATFORM_NAME)
PRODUCT_DESCRIPTION_TEMPLATE = """Product Name: {product_name}

Price: From R${current_price} to R${old_price} ({discount})

Description: {description}

🛒 Encontre na {platform}:
👉 {url}"""

# Sound
SOUND_COMMANDS = {
    "Darwin": "afplay",
    "Linux": "aplay",
    "Windows": "start",
}
SOUND_FILE = "./.assets/Sounds/NotificationSound.wav"

RUN_FUNCTIONS = {
    "Play Sound": True,
}


# =============================================================================
# BackgroundColors
# =============================================================================

class BackgroundColors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    CLEAR_TERMINAL = "\033[H\033[J"


# =============================================================================
# Module-level Utility Functions
# =============================================================================

def verbose_output(true_string="", false_string=""):
    """Output message if VERBOSE is True, else output false_string if provided."""
    if VERBOSE and true_string:
        print(true_string)
    elif false_string:
        print(false_string)


def output_result(result, platform_name="this platform"):
    """Display scraping result summary to terminal."""
    if result:
        print(
            f"{BackgroundColors.GREEN}Scraping successful! Product data:{Style.RESET_ALL}\n"
            f"  {BackgroundColors.CYAN}Name:{Style.RESET_ALL} {result.get('name', 'N/A')}\n"
            f"  {BackgroundColors.CYAN}Price:{Style.RESET_ALL} R${result.get('current_price_integer', 'N/A')},"
            f"{result.get('current_price_decimal', 'N/A')}\n"
            f"  {BackgroundColors.CYAN}Files:{Style.RESET_ALL} {len(result.get('downloaded_files', []))} downloaded"
        )
    else:
        print(f"{BackgroundColors.RED}Scraping failed. No data returned.{Style.RESET_ALL}")


def verify_filepath_exists(filepath):
    """Return True if the file or directory exists."""
    verbose_output(
        f"{BackgroundColors.GREEN}Verifying path: {BackgroundColors.CYAN}{filepath}{Style.RESET_ALL}"
    )
    return os.path.exists(filepath)


def verify_dot_env_file():
    """Return True if .env file exists in the script's directory."""
    env_path = Path(__file__).parent / ".env"
    if not verify_filepath_exists(env_path):
        print(
            f"{BackgroundColors.CYAN}.env{BackgroundColors.YELLOW} file not found at "
            f"{BackgroundColors.CYAN}{env_path}{BackgroundColors.YELLOW}.{Style.RESET_ALL}"
        )
        return False
    return True


def to_seconds(obj):
    """Convert time-like objects to seconds (float)."""
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if hasattr(obj, "total_seconds"):
        try:
            return float(obj.total_seconds())
        except Exception:
            pass
    if hasattr(obj, "timestamp"):
        try:
            return float(obj.timestamp())
        except Exception:
            pass
    return None


def calculate_execution_time(start_time, finish_time=None):
    """Calculate and format execution time as human-readable string."""
    if finish_time is None:
        total_seconds = to_seconds(start_time)
        if total_seconds is None:
            try:
                total_seconds = float(start_time)
            except Exception:
                total_seconds = 0.0
    else:
        st = to_seconds(start_time)
        ft = to_seconds(finish_time)
        if st is not None and ft is not None:
            total_seconds = ft - st
        else:
            try:
                delta = finish_time - start_time
                total_seconds = float(delta.total_seconds())
            except Exception:
                try:
                    total_seconds = float(finish_time) - float(start_time)
                except Exception:
                    total_seconds = 0.0

    if total_seconds is None:
        total_seconds = 0.0
    if total_seconds < 0:
        total_seconds = abs(total_seconds)

    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)

    if days > 0:
        return f"{days}d {hours}h {minutes}m {seconds}s"
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def play_sound():
    """Play notification sound (skipped on Windows)."""
    current_os = platform.system()
    if current_os == "Windows":
        return
    if verify_filepath_exists(SOUND_FILE):
        if current_os in SOUND_COMMANDS:
            os.system(f"{SOUND_COMMANDS[current_os]} {SOUND_FILE}")
        else:
            print(
                f"{BackgroundColors.RED}{current_os} not in SOUND_COMMANDS.{Style.RESET_ALL}"
            )
    else:
        print(
            f"{BackgroundColors.RED}Sound file {SOUND_FILE} not found.{Style.RESET_ALL}"
        )


# =============================================================================
# BaseScraper
# =============================================================================

class BaseScraper:
    """
    Platform-agnostic base scraper for e-commerce product pages.

    Subclasses must set PLATFORM_NAME and implement the extraction methods.
    The scrape() method orchestrates: launch → load → render → extract → download.
    """

    PLATFORM_NAME: str = "this platform"

    def __init__(
        self,
        url: str = "",
        local_html_path: Optional[str] = None,
        prefix: str = "",
        output_directory: str = OUTPUT_DIRECTORY,
        external_page: Optional[Any] = None,
        skip_media: bool = False,
    ):
        self.url = url
        self.product_url = url
        self.local_html_path = local_html_path
        self.html_content: Optional[str] = None
        self.product_data: dict = {}
        self.prefix = prefix
        self.output_directory = output_directory
        self.playwright: Optional[Any] = None
        self.browser: Optional[Any] = None
        self.page: Optional[Any] = None
        self.external_page = external_page  # Shared page for batch scraping
        self.skip_media = skip_media  # Skip image/video/snapshot download
        self._intercepted_data: dict = {}  # API response cache
        self._api_patterns: list = []  # URL substrings to intercept

        verbose_output(
            f"{BackgroundColors.GREEN}{self.PLATFORM_NAME} scraper initialized with URL: "
            f"{BackgroundColors.CYAN}{url}{Style.RESET_ALL}"
        )
        if local_html_path:
            verbose_output(
                f"{BackgroundColors.GREEN}Offline mode. Reading from: "
                f"{BackgroundColors.CYAN}{local_html_path}{Style.RESET_ALL}"
            )

    # ── Browser Lifecycle ───────────────────────────────────────────────

    # Stealth viewport pool
    _VIEWPORTS = [
        {"width": 1920, "height": 1080},
        {"width": 1366, "height": 768},
        {"width": 1536, "height": 864},
        {"width": 1440, "height": 900},
        {"width": 1600, "height": 900},
    ]

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def _apply_stealth(self):
        """Apply anti-detection measures to avoid bot fingerprinting."""
        if self.page is None:
            return
        # Hide webdriver property
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            // Overwrite chrome object
            window.chrome = { runtime: {} };
            // Overwrite permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({state: Notification.permission}) :
                originalQuery(parameters)
            );
        """)

    def launch_browser(self):
        """Launch stealth Chrome browser via Playwright (or reuse shared page)."""
        verbose_output(
            f"{BackgroundColors.GREEN}Launching stealth Chrome...{Style.RESET_ALL}"
        )
        try:
            if self.external_page:
                self.page = self.external_page
                self.playwright = None
                self.browser = None
                verbose_output(
                    f"{BackgroundColors.GREEN}Using shared page.{Style.RESET_ALL}"
                )
                return
            self.playwright = sync_playwright().start()
            vp = random.choice(self._VIEWPORTS)

            if CHROME_PROFILE_PATH:
                # Use persistent context with cloakbrowser (keeps login cookies)
                verbose_output(
                    f"{BackgroundColors.GREEN}Using profile: "
                    f"{BackgroundColors.CYAN}{CHROME_PROFILE_PATH}{Style.RESET_ALL}"
                )
                persistent_opts = {
                    "user_data_dir": CHROME_PROFILE_PATH,
                    "headless": False,
                    "viewport": vp,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                }
                if CHROME_EXECUTABLE_PATH:
                    persistent_opts["executable_path"] = CHROME_EXECUTABLE_PATH
                    verbose_output(
                        f"{BackgroundColors.GREEN}Using executable: "
                        f"{BackgroundColors.CYAN}{CHROME_EXECUTABLE_PATH}{Style.RESET_ALL}"
                    )
                if PROXY_SERVER:
                    persistent_opts["proxy"] = {"server": PROXY_SERVER}
                    verbose_output(
                        f"{BackgroundColors.GREEN}Using proxy: "
                        f"{BackgroundColors.CYAN}{PROXY_SERVER.split('@')[-1] if '@' in PROXY_SERVER else PROXY_SERVER}{Style.RESET_ALL}"
                    )
                context = self.playwright.chromium.launch_persistent_context(**persistent_opts)
                self.page = context.pages[0] if context.pages else context.new_page()
                self._context = context
                self.browser = None
            else:
                launch_options = {
                    "headless": HEADLESS,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                }
                if CHROME_EXECUTABLE_PATH:
                    launch_options["executable_path"] = CHROME_EXECUTABLE_PATH
                self.browser = self.playwright.chromium.launch(**launch_options)
                if self.browser is None:
                    raise Exception("Failed to initialize browser")
                self.page = self.browser.new_page()
                self.page.set_viewport_size(vp)

            if self.page is None:
                raise Exception("Failed to create page")
            # Apply stealth init scripts
            self._apply_stealth()
            verbose_output(
                f"{BackgroundColors.GREEN}Browser launched (stealth).{Style.RESET_ALL}"
            )
        except Exception as e:
            print(f"{BackgroundColors.RED}Failed to launch browser: {e}{Style.RESET_ALL}")
            raise

    def close_browser(self):
        """Safely close browser and Playwright (skip if using external page)."""
        verbose_output(f"{BackgroundColors.GREEN}Closing browser...{Style.RESET_ALL}")
        try:
            if self.external_page:
                return  # Don't close shared page
            if hasattr(self, '_context') and self._context:
                self._context.close()
            elif self.page:
                self.page.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
            verbose_output(f"{BackgroundColors.GREEN}Browser closed.{Style.RESET_ALL}")
        except Exception as e:
            print(f"{BackgroundColors.YELLOW}Warning during close: {e}{Style.RESET_ALL}")

    # ── Page Loading ────────────────────────────────────────────────────

    def load_page(self) -> bool:
        """Navigate to product URL. Only waits for DOM (not network idle —
        AliExpress pages load third-party tracking scripts indefinitely)."""
        verbose_output(
            f"{BackgroundColors.GREEN}Loading: {BackgroundColors.CYAN}{self.product_url}{Style.RESET_ALL}"
        )
        if self.page is None:
            print(f"{BackgroundColors.RED}Page not initialized.{Style.RESET_ALL}")
            return False
        try:
            self.page.goto(
                self.product_url,
                timeout=PAGE_LOAD_TIMEOUT,
                wait_until="domcontentloaded",
            )
            # Skip networkidle — AliExpress tracking scripts never settle
            time.sleep(3)  # Brief pause for lazy DOM hydration
            # Verify we're on the right page (not a stale/cached page)
            current_url = self.page.url
            if "/item/" not in current_url:
                print(f"{BackgroundColors.RED}Page redirected away from product.{Style.RESET_ALL}")
                return False
            verbose_output(f"{BackgroundColors.GREEN}Page loaded (dom only).{Style.RESET_ALL}")
            return True
        except PlaywrightTimeoutError:
            print(
                f"{BackgroundColors.YELLOW}Page load timeout.{Style.RESET_ALL}"
            )
            return False
        except Exception as e:
            print(f"{BackgroundColors.RED}Failed to load page: {e}{Style.RESET_ALL}")
            return False

    def auto_scroll(self):
        """Scroll page to trigger lazy-loaded content."""
        verbose_output(
            f"{BackgroundColors.GREEN}Auto-scrolling for lazy content...{Style.RESET_ALL}"
        )
        if self.page is None:
            print(
                f"{BackgroundColors.YELLOW}Page not initialized, skipping scroll.{Style.RESET_ALL}"
            )
            return
        try:
            previous_height = self.page.evaluate("document.body.scrollHeight")
            while True:
                self.page.evaluate(f"window.scrollBy(0, {SCROLL_STEP})")
                time.sleep(SCROLL_PAUSE_TIME)
                new_height = self.page.evaluate("document.body.scrollHeight")
                scroll_position = self.page.evaluate(
                    "window.pageYOffset + window.innerHeight"
                )
                if scroll_position >= new_height:
                    break
                if new_height == previous_height:
                    break
                previous_height = new_height
            self.page.evaluate("window.scrollTo(0, 0)")
            time.sleep(SCROLL_PAUSE_TIME)
            verbose_output(f"{BackgroundColors.GREEN}Auto-scroll done.{Style.RESET_ALL}")
        except Exception as e:
            print(f"{BackgroundColors.YELLOW}Scroll warning: {e}{Style.RESET_ALL}")

    def wait_full_render(self):
        """Wait for key selectors to become visible."""
        verbose_output(
            f"{BackgroundColors.GREEN}Waiting for full render...{Style.RESET_ALL}"
        )
        if self.page is None:
            print(
                f"{BackgroundColors.YELLOW}Page not initialized, skipping render wait.{Style.RESET_ALL}"
            )
            return
        try:
            selectors_to_wait = ["h1", "div[class*='price']", "img"]
            for sel in selectors_to_wait:
                try:
                    self.page.wait_for_selector(sel, timeout=5000, state="visible")
                except Exception:
                    pass
            time.sleep(2)
            verbose_output(f"{BackgroundColors.GREEN}Page fully rendered.{Style.RESET_ALL}")
        except Exception as e:
            print(f"{BackgroundColors.YELLOW}Render wait warning: {e}{Style.RESET_ALL}")

    # ── HTML Content ────────────────────────────────────────────────────

    def get_rendered_html(self) -> Optional[str]:
        """Extract fully rendered HTML after JS execution."""
        verbose_output(f"{BackgroundColors.GREEN}Extracting HTML...{Style.RESET_ALL}")
        if self.page is None:
            print(f"{BackgroundColors.RED}Page not initialized.{Style.RESET_ALL}")
            return None
        try:
            html = self.page.content()
            verbose_output(f"{BackgroundColors.GREEN}HTML extracted.{Style.RESET_ALL}")
            return html
        except Exception as e:
            print(f"{BackgroundColors.RED}HTML extraction failed: {e}{Style.RESET_ALL}")
            return None

    def read_local_html(self) -> Optional[str]:
        """Read HTML from local file for offline scraping."""
        verbose_output(
            f"{BackgroundColors.GREEN}Reading local HTML: "
            f"{BackgroundColors.CYAN}{self.local_html_path}{Style.RESET_ALL}"
        )
        try:
            if not self.local_html_path:
                print(f"{BackgroundColors.RED}No local HTML path.{Style.RESET_ALL}")
                return None
            if not os.path.exists(self.local_html_path):
                print(
                    f"{BackgroundColors.RED}Local HTML not found: "
                    f"{BackgroundColors.CYAN}{self.local_html_path}{Style.RESET_ALL}"
                )
                return None
            with open(self.local_html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            verbose_output(f"{BackgroundColors.GREEN}Local HTML loaded.{Style.RESET_ALL}")
            return html_content
        except Exception as e:
            print(f"{BackgroundColors.RED}Error reading local HTML: {e}{Style.RESET_ALL}")
            return None

    # ── Currency Normalization ──────────────────────────────────────────

    def normalize_brazilian_currency(
        self, price_text: str
    ) -> Optional[Tuple[str, str]]:
        """
        Normalize Brazilian currency: "R$2.299,08" -> ("2299", "08")
        """
        if not price_text:
            return None
        normalized = price_text.strip()
        normalized = re.sub(r"[R$€£¥]", "", normalized)
        normalized = normalized.replace(" ", " ").strip()
        match = re.search(r"([0-9.]+)[,.]([0-9]{2})", normalized)
        if not match:
            return None
        integer_part = match.group(1).replace(".", "").replace(",", "")
        decimal_part = match.group(2)
        if not integer_part or not integer_part.isdigit():
            return None
        if not decimal_part.isdigit() or len(decimal_part) != 2:
            return None
        return integer_part, decimal_part

    # ── File/Directory Helpers ──────────────────────────────────────────

    def create_directory(self, full_directory_name: str, relative_directory_name: str):
        """Create directory if it doesn't exist."""
        verbose_output(
            f"{BackgroundColors.GREEN}Creating "
            f"{BackgroundColors.CYAN}{relative_directory_name}{BackgroundColors.GREEN} "
            f"directory...{Style.RESET_ALL}"
        )
        if os.path.isdir(full_directory_name):
            return
        try:
            os.makedirs(full_directory_name)
        except OSError:
            print(
                f"{BackgroundColors.GREEN}Failed to create "
                f"{BackgroundColors.CYAN}{relative_directory_name}{BackgroundColors.GREEN} "
                f"directory.{Style.RESET_ALL}"
            )

    def create_output_directory(self, product_name_safe: str) -> str:
        """Create per-product output directory."""
        raw = (
            f"{self.prefix} - {product_name_safe}"
            if self.prefix
            else product_name_safe
        )
        directory_name = normalize_product_name(raw)
        output_dir = os.path.join(self.output_directory, directory_name)
        self.create_directory(
            os.path.abspath(output_dir), output_dir.replace(".", "")
        )
        return output_dir

    # ── Text Cleaning ───────────────────────────────────────────────────

    def clean_description(self, text: str) -> str:
        """Remove markdown formatting and excessive blank lines."""
        if not text:
            return text
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            cl = line.strip()
            if cl or (cleaned and cleaned[-1]):
                cleaned.append(cl)
        text = "\n".join(cleaned)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def to_sentence_case(self, text: str) -> str:
        """Convert text to sentence case."""
        if not text:
            return text
        sentences = re.split(r"([.!?]\s*)", text)
        result = []
        for i, sentence in enumerate(sentences):
            if sentence.strip():
                if i % 2 == 0:
                    sentence = sentence.strip()
                    if sentence:
                        sentence = sentence[0].upper() + sentence[1:].lower()
                result.append(sentence)
        return "".join(result)

    # ── Media Download ──────────────────────────────────────────────────

    def download_single_image(
        self, img_url: str, output_dir: str, index: int
    ) -> Optional[str]:
        """
        Download a single image.
        Uses requests (faster, more compatible) for HTTP; shutil for local files.
        """
        try:
            # ── Local file mode ──
            if self.local_html_path and not img_url.startswith(
                ("http://", "https://")
            ):
                html_dir = os.path.dirname(os.path.abspath(self.local_html_path))
                source_path = os.path.join(html_dir, img_url.lstrip("./"))
                if not os.path.exists(source_path):
                    verbose_output(
                        f"{BackgroundColors.RED}Local image missing: {source_path}{Style.RESET_ALL}"
                    )
                    return None
                _, ext = os.path.splitext(source_path)
                if not ext or ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                basename = os.path.splitext(os.path.basename(source_path))[0]
                dest = os.path.join(output_dir, f"{index:02d}_{basename}{ext}")
                shutil.copy2(source_path, dest)
                verbose_output(
                    f"{BackgroundColors.GREEN}Copied image {index}: "
                    f"{BackgroundColors.CYAN}{os.path.basename(dest)}{Style.RESET_ALL}"
                )
                return dest

            # ── HTTP download ──
            if not img_url.startswith(("http://", "https://")):
                img_url = "https:" + img_url if img_url.startswith("//") else img_url
            resp = requests.get(img_url, timeout=30)
            resp.raise_for_status()

            ext = os.path.splitext(urlparse(img_url).path)[1]
            if not ext or ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ct = resp.headers.get("content-type", "")
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                elif "gif" in ct:
                    ext = ".gif"
                else:
                    ext = ".jpg"

            basename = os.path.splitext(os.path.basename(urlparse(img_url).path))[0]
            dest = os.path.join(output_dir, f"{index:02d}_{basename}{ext}")
            with open(dest, "wb") as f:
                f.write(resp.content)
            verbose_output(
                f"{BackgroundColors.GREEN}Downloaded image {index}{Style.RESET_ALL}"
            )
            return dest
        except Exception as e:
            verbose_output(
                f"{BackgroundColors.RED}Error downloading image {index}: {e}{Style.RESET_ALL}"
            )
            return None

    def download_single_video(
        self, video_url: str, output_dir: str, index: int
    ) -> Optional[str]:
        """Download video. Supports HLS (.m3u8) via ffmpeg, HTTP, and local copy."""
        try:
            # ── Local file mode ──
            if self.local_html_path and not video_url.startswith(
                ("http://", "https://")
            ):
                html_dir = os.path.dirname(os.path.abspath(self.local_html_path))
                source_path = os.path.join(html_dir, video_url.lstrip("./"))
                if not os.path.exists(source_path):
                    verbose_output(
                        f"{BackgroundColors.RED}Local video missing: {source_path}{Style.RESET_ALL}"
                    )
                    return None
                _, ext = os.path.splitext(source_path)
                if not ext:
                    ext = ".mp4"
                dest = os.path.join(output_dir, f"video_{index:03d}{ext}")
                shutil.copy2(source_path, dest)
                verbose_output(
                    f"{BackgroundColors.GREEN}Copied video {index}{Style.RESET_ALL}"
                )
                return dest

            # ── HLS stream ──
            if ".m3u8" in video_url:
                verbose_output(
                    f"{BackgroundColors.CYAN}HLS stream detected, using ffmpeg...{Style.RESET_ALL}"
                )
                dest = os.path.join(output_dir, f"video_{index:03d}.mp4")
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-i", video_url, "-c", "copy",
                            "-bsf:a", "aac_adtstoasc", "-y", dest,
                        ],
                        capture_output=True, text=True, timeout=300,
                    )
                    if result.returncode == 0 and os.path.exists(dest):
                        verbose_output(
                            f"{BackgroundColors.GREEN}HLS video {index} downloaded.{Style.RESET_ALL}"
                        )
                        return dest
                    else:
                        verbose_output(
                            f"{BackgroundColors.RED}ffmpeg failed: {result.stderr}{Style.RESET_ALL}"
                        )
                        return None
                except FileNotFoundError:
                    print(
                        f"{BackgroundColors.RED}ffmpeg not found. Install ffmpeg for HLS.{Style.RESET_ALL}"
                    )
                    return None
                except subprocess.TimeoutExpired:
                    print(f"{BackgroundColors.RED}ffmpeg timeout (5 min).{Style.RESET_ALL}")
                    return None

            # ── Direct HTTP download ──
            if not video_url.startswith(("http://", "https://")):
                video_url = (
                    "https:" + video_url
                    if video_url.startswith("//")
                    else "https:" + video_url
                )
            resp = requests.get(video_url, timeout=60, stream=True)
            resp.raise_for_status()
            ext = os.path.splitext(urlparse(video_url).path)[1] or ".mp4"
            dest = os.path.join(output_dir, f"video_{index:03d}{ext}")
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            verbose_output(
                f"{BackgroundColors.GREEN}Downloaded video {index}{Style.RESET_ALL}"
            )
            return dest
        except Exception as e:
            verbose_output(
                f"{BackgroundColors.RED}Error downloading video {index}: {e}{Style.RESET_ALL}"
            )
            return None

    def download_product_images(
        self, image_urls: List[str], output_dir: str
    ) -> List[str]:
        """Download all product images."""
        downloaded = []
        if not image_urls:
            verbose_output(f"{BackgroundColors.YELLOW}No image URLs.{Style.RESET_ALL}")
            return downloaded
        verbose_output(
            f"{BackgroundColors.GREEN}Downloading {len(image_urls)} images...{Style.RESET_ALL}"
        )
        for idx, url in enumerate(image_urls, start=1):
            path = self.download_single_image(url, output_dir, idx)
            if path:
                downloaded.append(path)
        verbose_output(
            f"{BackgroundColors.GREEN}{len(downloaded)}/{len(image_urls)} "
            f"images downloaded.{Style.RESET_ALL}"
        )
        return downloaded

    def download_product_videos(
        self, video_urls: List[str], output_dir: str
    ) -> List[str]:
        """Download all product videos."""
        downloaded = []
        if not video_urls:
            verbose_output(f"{BackgroundColors.YELLOW}No video URLs.{Style.RESET_ALL}")
            return downloaded
        verbose_output(
            f"{BackgroundColors.GREEN}Downloading {len(video_urls)} videos...{Style.RESET_ALL}"
        )
        for idx, url in enumerate(video_urls, start=1):
            path = self.download_single_video(url, output_dir, idx)
            if path:
                downloaded.append(path)
        verbose_output(
            f"{BackgroundColors.GREEN}{len(downloaded)}/{len(video_urls)} "
            f"videos downloaded.{Style.RESET_ALL}"
        )
        return downloaded

    # ── Assets & Snapshot ──────────────────────────────────────────────

    def collect_assets(
        self, html_content: str, output_dir: str
    ) -> dict:
        """Download page assets (images, CSS, JS) for offline snapshot."""
        verbose_output(f"{BackgroundColors.GREEN}Collecting assets...{Style.RESET_ALL}")
        if self.page is None:
            return {}
        assets_dir = os.path.join(output_dir, "assets")
        self.create_directory(assets_dir, "assets")
        asset_map = {}
        soup = BeautifulSoup(html_content, "html.parser")
        img_tags = soup.find_all("img", src=True)
        for idx, img in enumerate(img_tags, 1):
            if not isinstance(img, Tag):
                continue
            src = img.get("src")
            if not src or not isinstance(src, str):
                continue
            absolute_url = urljoin(self.product_url, src)
            try:
                response = self.page.goto(absolute_url, timeout=10000)
                if response and response.ok:
                    parsed = urlparse(absolute_url)
                    ext = os.path.splitext(parsed.path)[1] or ".jpg"
                    basename = os.path.splitext(os.path.basename(parsed.path))[0]
                    filename = f"{idx:02d}_{basename}{ext}"
                    filepath = os.path.join(assets_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(response.body())
                    asset_map[src] = f"assets/{filename}"
                    verbose_output(
                        f"{BackgroundColors.GREEN}Asset: {filename}{Style.RESET_ALL}"
                    )
            except Exception as e:
                verbose_output(
                    f"{BackgroundColors.YELLOW}Asset failed: {src} - {e}{Style.RESET_ALL}"
                )
        verbose_output(
            f"{BackgroundColors.GREEN}Collected {len(asset_map)} assets.{Style.RESET_ALL}"
        )
        return asset_map

    def save_snapshot(
        self,
        html_content: str,
        output_dir: str,
        asset_map: Optional[dict] = None,
    ) -> Optional[str]:
        """Save page HTML with localized asset references."""
        verbose_output(f"{BackgroundColors.GREEN}Saving snapshot...{Style.RESET_ALL}")
        if asset_map is None:
            asset_map = {}
        try:
            modified_html = html_content
            for original_url, local_path in asset_map.items():
                modified_html = modified_html.replace(original_url, local_path)
            snapshot_path = os.path.join(output_dir, "page.html")
            with open(snapshot_path, "w", encoding="utf-8") as f:
                f.write(modified_html)
            verbose_output(
                f"{BackgroundColors.GREEN}Snapshot: {snapshot_path}{Style.RESET_ALL}"
            )
            return snapshot_path
        except Exception as e:
            print(f"{BackgroundColors.RED}Snapshot failed: {e}{Style.RESET_ALL}")
            return None

    def create_product_description_file(
        self,
        product_data: dict,
        output_dir: str,
        product_name_safe: str,
        url: str,
    ) -> Optional[str]:
        """Create formatted product description .txt file."""
        try:
            product_name = product_data.get("name", "Produto")
            if isinstance(product_name, str):
                product_name = product_name.title()

            if (
                isinstance(product_name, str)
                and product_name.strip().lower() == "unknown product"
            ):
                verbose_output(
                    f"{BackgroundColors.YELLOW}Skipping description for Unknown Product.{Style.RESET_ALL}"
                )
                return None

            description = product_data.get("description", "")
            if description:
                description = self.clean_description(description)
                description = self.to_sentence_case(description)

            old_int = product_data.get("old_price_integer", "0")
            old_dec = product_data.get("old_price_decimal", "00")
            cur_int = product_data.get("current_price_integer", "0")
            cur_dec = product_data.get("current_price_decimal", "00")
            discount = product_data.get("discount_percentage", "N/A")

            old_price = f"{old_int},{old_dec}" if old_int != "N/A" else "N/A"
            current_price = f"{cur_int},{cur_dec}"

            template = PRODUCT_DESCRIPTION_TEMPLATE.replace(
                "{platform}", self.PLATFORM_NAME
            )
            content = template.format(
                product_name=product_name,
                current_price=current_price,
                old_price=old_price,
                discount=discount,
                description=description,
                url=url,
            )

            txt_filename = f"{product_name_safe}_description.txt"
            txt_path = os.path.join(output_dir, txt_filename)
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(content)

            verbose_output(
                f"{BackgroundColors.GREEN}Created: "
                f"{BackgroundColors.CYAN}{txt_filename}{Style.RESET_ALL}"
            )
            return txt_path
        except Exception as e:
            print(
                f"{BackgroundColors.YELLOW}Description file error: {e}{Style.RESET_ALL}"
            )
            return None

    # ── Info Output ─────────────────────────────────────────────────────

    def print_product_info(self, product_data: Optional[dict] = None):
        """Print extracted product info to terminal."""
        if not product_data:
            print(f"{BackgroundColors.RED}No product data.{Style.RESET_ALL}")
            return
        old_int = product_data.get("old_price_integer", "N/A")
        old_dec = product_data.get("old_price_decimal", "N/A")
        old_display = f"{old_int},{old_dec}" if old_int != "N/A" else "N/A"
        verbose_output(
            f"{BackgroundColors.GREEN}Product info:{BackgroundColors.GREEN}\n"
            f"  {BackgroundColors.CYAN}Name:{BackgroundColors.GREEN} {product_data.get('name', 'N/A')}\n"
            f"  {BackgroundColors.CYAN}Old Price:{BackgroundColors.GREEN} R${old_display}\n"
            f"  {BackgroundColors.CYAN}Current Price:{BackgroundColors.GREEN} R${product_data.get('current_price_integer', 'N/A')},"
            f"{product_data.get('current_price_decimal', 'N/A')}\n"
            f"  {BackgroundColors.CYAN}Discount:{BackgroundColors.GREEN} {product_data.get('discount_percentage', 'N/A')}\n"
            f"  {BackgroundColors.CYAN}Description:{BackgroundColors.GREEN} "
            f"{str(product_data.get('description', 'N/A'))[:100]}...{Style.RESET_ALL}"
        )

    # ── Core Pipeline ───────────────────────────────────────────────────

    def download_media(self) -> List[str]:
        """Download product images, videos, assets, snapshot, and description."""
        verbose_output(f"{BackgroundColors.GREEN}Processing media...{Style.RESET_ALL}")
        downloaded = []
        try:
            if not self.product_data or not self.product_data.get("name"):
                print(f"{BackgroundColors.RED}No product data for media.{Style.RESET_ALL}")
                return downloaded

            product_name = self.product_data.get("name", "Unknown Product")
            html_content = self.html_content
            if not html_content:
                print(f"{BackgroundColors.RED}No HTML content.{Style.RESET_ALL}")
                return downloaded

            soup = BeautifulSoup(html_content, "lxml")

            # Let subclass detect international and optionally prefix name
            is_international = self.detect_international(soup)
            if is_international and not product_name.startswith("International"):
                product_name = f"International - {product_name}"
                self.product_data["name"] = product_name
                self._on_international_detected(product_name)

            product_name_safe = normalize_product_name(product_name)
            output_dir = self.create_output_directory(product_name_safe)
            self.product_data["product_name_safe"] = os.path.basename(output_dir)

            # Images
            image_urls = self.find_image_urls(soup)
            if image_urls:
                verbose_output(
                    f"{BackgroundColors.GREEN}Found {len(image_urls)} images.{Style.RESET_ALL}"
                )
                downloaded.extend(self.download_product_images(image_urls, output_dir))
            else:
                verbose_output(f"{BackgroundColors.YELLOW}No gallery images.{Style.RESET_ALL}")

            # Videos
            video_urls = self.find_video_urls(soup)
            if video_urls:
                verbose_output(
                    f"{BackgroundColors.GREEN}Found {len(video_urls)} videos.{Style.RESET_ALL}"
                )
                downloaded.extend(self.download_product_videos(video_urls, output_dir))
            else:
                verbose_output(f"{BackgroundColors.YELLOW}No gallery videos.{Style.RESET_ALL}")

            # Snapshot + assets (online only)
            if not self.local_html_path:
                asset_map = self.collect_assets(html_content, output_dir)
                snapshot = self.save_snapshot(html_content, output_dir, asset_map)
                if snapshot:
                    downloaded.append(snapshot)

            # Description file
            desc_file = self.create_product_description_file(
                self.product_data,
                output_dir,
                self.product_data["product_name_safe"],
                self.product_url,
            )
            if desc_file:
                downloaded.append(desc_file)

            verbose_output(
                f"{BackgroundColors.GREEN}Media done. "
                f"{BackgroundColors.CYAN}{len(downloaded)}{BackgroundColors.GREEN} files.{Style.RESET_ALL}"
            )
        except Exception as e:
            print(f"{BackgroundColors.RED}Media error: {e}{Style.RESET_ALL}")
        return downloaded

    def scrape_product_info(self, html_content: str) -> Optional[dict]:
        """
        Parse HTML and extract product info.

        Default implementation calls the individual extract_* methods.
        Subclasses may override entirely if the flow differs.
        """
        verbose_output(f"{BackgroundColors.GREEN}Parsing product info...{Style.RESET_ALL}")
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            product_name = self.extract_product_name(soup)
            is_international = self.detect_international(soup)
            if is_international:
                product_name = self.prefix_international_name(product_name)
            cur_int, cur_dec = self.extract_current_price(soup)
            discount = self.extract_discount_percentage(soup)
            old_int, old_dec = self.extract_old_price(soup, cur_int, cur_dec, discount)
            description = self.extract_product_description(soup)

            self.product_data = {
                "name": product_name,
                "is_international": is_international,
                "current_price_integer": cur_int,
                "current_price_decimal": cur_dec,
                "old_price_integer": old_int,
                "old_price_decimal": old_dec,
                "discount_percentage": discount,
                "description": description,
                "url": self.product_url,
            }
            self.print_product_info(self.product_data)
            return self.product_data
        except Exception as e:
            print(f"{BackgroundColors.RED}Parse error: {e}{Style.RESET_ALL}")
            return None

    # ── API Interception ────────────────────────────────────────────

    def _setup_api_interception(self):
        """Listen for API responses and capture structured product data.

        Subclasses should set self._api_patterns to a list of URL substrings
        to match (e.g. ['mtop.aliexpress', 'getDetailProduct']).
        Captured responses are stored in self._intercepted_data keyed by
        the matched pattern.
        """
        if self.page is None or not self._api_patterns:
            return

        intercepted = self._intercepted_data

        def on_response(response):
            url = response.url
            for pattern in self._api_patterns:
                if pattern in url:
                    try:
                        body = response.json()
                    except Exception:
                        # Try text — may be JSONP
                        try:
                            text = response.text()
                            # JSONP: callback({...}) → extract JSON
                            m = re.search(r'[\{].*[\}]', text, re.DOTALL)
                            if m:
                                body = json.loads(m.group())
                            else:
                                continue
                        except Exception:
                            continue
                    if body and isinstance(body, dict):
                        if pattern in intercepted:
                            if isinstance(intercepted[pattern], list):
                                intercepted[pattern].append(body)
                            else:
                                intercepted[pattern] = [intercepted[pattern], body]
                        else:
                            intercepted[pattern] = body
                        verbose_output(
                            f"{BackgroundColors.GREEN}API captured: "
                            f"{BackgroundColors.CYAN}{pattern}{Style.RESET_ALL}"
                        )
                    break

        self.page.on("response", on_response)

    def _extract_from_api(self) -> Optional[dict]:
        """Extract product data from intercepted API responses.

        Subclasses override this to parse self._intercepted_data into
        the standard product_data format. Return None if no API data
        was captured (will fall back to HTML parsing).
        """
        return None

    def scrape(self, verbose: bool = False) -> Optional[dict]:
        """
        Main orchestrator. Works online (browser) and offline (local HTML).

        Returns dict with product data + downloaded_files list.
        """
        verbose_output(
            f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
            f"Starting {BackgroundColors.CYAN}{self.PLATFORM_NAME}"
            f"{BackgroundColors.GREEN} scraping...{Style.RESET_ALL}"
        )
        try:
            if self.local_html_path:
                verbose_output(
                    f"{BackgroundColors.GREEN}Offline mode.{Style.RESET_ALL}"
                )
                html_content = self.read_local_html()
                if not html_content:
                    return None
                self.html_content = html_content
            else:
                verbose_output(
                    f"{BackgroundColors.GREEN}Online mode.{Style.RESET_ALL}"
                )
                self.launch_browser()
                # Setup API interception BEFORE page load
                self._intercepted_data = {}
                self._setup_api_interception()
                if not self.load_page():
                    return None
                self.wait_full_render()
                self.auto_scroll()
                html_content = self.get_rendered_html()
                if not html_content:
                    return None
                self.html_content = html_content

            # Try API-first extraction, fall back to HTML
            product_info = self._extract_from_api()
            if product_info:
                verbose_output(
                    f"{BackgroundColors.GREEN}Using API-extracted data.{Style.RESET_ALL}"
                )
                self.product_data = product_info
            else:
                product_info = self.scrape_product_info(html_content)
            if not product_info:
                return None

            if self.skip_media:
                downloaded_files = []
            else:
                downloaded_files = self.download_media()
            product_info["downloaded_files"] = downloaded_files

            verbose_output(
                f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
                f"{self.PLATFORM_NAME} scraping completed!{Style.RESET_ALL}"
            )
            return product_info
        except Exception as e:
            print(
                f"{BackgroundColors.RED}Scraping failed: {e}{Style.RESET_ALL}"
            )
            return None
        finally:
            if not self.local_html_path:
                self.close_browser()

    # ── Hooks for subclasses ────────────────────────────────────────────

    def extract_product_name(self, soup) -> str:
        raise NotImplementedError

    def extract_current_price(self, soup) -> Tuple[str, str]:
        raise NotImplementedError

    def extract_old_price(
        self,
        soup,
        current_price_int: str = "0",
        current_price_dec: str = "00",
        discount_percentage: str = "N/A",
    ) -> Tuple[str, str]:
        raise NotImplementedError

    def extract_discount_percentage(self, soup) -> str:
        raise NotImplementedError

    def extract_product_description(self, soup):
        """Returns str or dict. Subclasses may return structured data."""
        raise NotImplementedError

    def find_image_urls(self, soup) -> List[str]:
        raise NotImplementedError

    def find_video_urls(self, soup) -> List[str]:
        raise NotImplementedError

    def detect_international(self, soup) -> bool:
        raise NotImplementedError

    def prefix_international_name(self, product_name: str) -> str:
        """Add 'International - ' prefix. Override if platform uses different logic."""
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
        """Called during download_media when product is detected as international."""
        verbose_output(
            f"{BackgroundColors.YELLOW}Product name prefixed 'International'.{Style.RESET_ALL}"
        )


# =============================================================================
# Test / Standalone
# =============================================================================

if __name__ == "__main__":
    print(
        f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
        f"BaseScraper — this module is not meant to be run directly."
        f"{Style.RESET_ALL}"
    )
    print("Import it and subclass BaseScraper for a specific platform.")
