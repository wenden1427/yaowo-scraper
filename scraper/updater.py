"""Auto-updater for 耀我科技采集器.

Checks GitHub for new commits on startup. If a newer version exists,
asks user whether to update before launching the main GUI.
"""

import os
import json
import urllib.request
import urllib.error
import zipfile
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox; import tempfile

REPO_API = "https://api.github.com/repos/wenden1427/yaowo-scraper/commits/main"
REPO_ZIP = "https://github.com/wenden1427/yaowo-scraper/archive/refs/heads/main.zip"
SCRAPER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(SCRAPER_DIR, ".."))
VERSION_FILE = os.path.join(SCRAPER_DIR, "version.txt")


def _get_local_version():
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "r") as f:
            return f.read().strip()
    return ""


def _get_remote_version():
    for use_proxy in (True, False):
        try:
            req = urllib.request.Request(REPO_API, headers={
                "User-Agent": "YaoWo-Scraper-Updater/1.0",
                "Accept": "application/vnd.github.v3+json",
            })
            opener = None
            if use_proxy:
                try:
                    import winreg
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
                    proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                    if proxy_enable:
                        proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                        server = str(proxy_server).split(";")[0].strip()
                        if "=" in server:
                            server = server.split("=", 1)[1].strip()
                        if server:
                            proxy_url = f"http://{server}" if "://" not in server else server
                            opener = urllib.request.build_opener(
                                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
                    winreg.CloseKey(key)
                except Exception:
                    pass
            if opener is None:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("sha", "")
        except Exception:
            continue
    return None


def _save_version(sha):
    with open(VERSION_FILE, "w") as f:
        f.write(sha)


def check_and_update(root):
    """Check for updates. If available, show dialog. Returns True if app should continue."""
    local = _get_local_version()
    remote = _get_remote_version()

    if not remote:
        return True  # Can't check, just continue

    if not local:
        # First run after update - just save version
        _save_version(remote)
        return True

    if local == remote:
        return True  # Up to date

    # Update available
    result = messagebox.askyesno(
        "发现新版本",
        f"采集器有新版本可用！\n\n当前: {local[:7]}...\n最新: {remote[:7]}...\n\n是否立即更新？\n(更新后会自动重启)",
    )
    if not result:
        return True  # User declined

    return _do_update(root)


def _do_update(root):
    """Download and apply update. Returns False to prevent old version from launching."""
    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "yaowo_scraper_update.zip")
        extract_dir = os.path.join(tempfile.gettempdir(), "yaowo_scraper_update_extract")

        # Download
        try:
            messagebox.showinfo("更新中", "正在下载更新...")
            req = urllib.request.Request(REPO_ZIP, headers={"User-Agent": "YaoWo-Scraper-Updater/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(tmp, "wb") as f:
                    f.write(resp.read())
        except Exception as e:
            messagebox.showerror("更新失败", f"下载失败: {e}")
            return True

        # Extract
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        with zipfile.ZipFile(tmp, "r") as zf:
            zf.extractall(extract_dir)

        # Find the inner directory (yaowo-scraper-main/)
        inner = os.path.join(extract_dir, os.listdir(extract_dir)[0])
        scraper_src = os.path.join(inner, "scraper")

        if not os.path.exists(scraper_src):
            messagebox.showerror("更新失败", "更新包结构异常")
            return True

        # Copy files
        for item in os.listdir(scraper_src):
            src = os.path.join(scraper_src, item)
            dst = os.path.join(SCRAPER_DIR, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        # Cleanup
        os.remove(tmp)
        shutil.rmtree(extract_dir)

        # Restart
        bat = os.path.join(ROOT_DIR, "启动采集器.bat")
        if os.path.exists(bat):
            subprocess.Popen(bat, shell=True)
        sys.exit(0)
    except Exception as e:
        messagebox.showerror("更新失败", str(e))
        return True
