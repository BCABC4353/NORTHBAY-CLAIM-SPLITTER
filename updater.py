from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable

import requests

from config import GITHUB_REPO
from splitter_engine import __version__

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 600
IDLE_POLL_SECONDS = 5
TIMEOUT_SECONDS = 30
CHUNK_BYTES = 1024 * 1024
USER_AGENT = "NorthBay-Claim-Splitter"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _version_tuple(text: str) -> tuple[int, ...]:
    value = text.strip()
    if value[:1] in ("v", "V"):
        value = value[1:]
    return tuple(int(part) for part in value.split("."))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", os.path.basename(path), type(exc).__name__)


class Updater:
    def __init__(
        self,
        is_idle: Callable[[], bool],
        on_status: Callable[[str], None],
        quit_app: Callable[[], None],
    ):
        self._is_idle = is_idle
        self._on_status = on_status
        self._quit_app = quit_app
        self._exe = os.path.abspath(sys.executable)
        self._part = self._exe + ".part"
        self._new = self._exe + ".new"
        self._old = self._exe + ".old"
        self._ready_version: str | None = None

    def start(self) -> None:
        if not getattr(sys, "frozen", False):
            return
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def _loop(self) -> None:
        try:
            _remove(self._old)
            _remove(self._part)
        except Exception as exc:
            logger.warning("Update cleanup failed: %s", type(exc).__name__)
        while True:
            try:
                if self._check():
                    return
            except Exception as exc:
                logger.warning("Update check failed: %s: %s", type(exc).__name__, exc)
            time.sleep(CHECK_INTERVAL_SECONDS)

    def _check(self) -> bool:
        resp = requests.get(
            LATEST_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        release = resp.json()
        tag = str(release.get("tag_name") or "")
        latest = tag[1:] if tag[:1] in ("v", "V") else tag
        if not latest or _version_tuple(latest) <= _version_tuple(__version__):
            return False
        asset = next(
            (a for a in release.get("assets") or [] if str(a.get("name") or "").lower().endswith(".exe")),
            None,
        )
        if asset is None:
            logger.warning("Release v%s has no .exe asset", latest)
            return False
        if self._ready_version != latest or not os.path.isfile(self._new):
            self._download(asset)
            self._ready_version = latest
            logger.info("Downloaded update v%s", latest)
        while not self._is_idle():
            time.sleep(IDLE_POLL_SECONDS)
        return self._apply(latest)

    def _download(self, asset: dict) -> None:
        expected = int(asset["size"])
        url = asset["browser_download_url"]
        _remove(self._part)
        try:
            with requests.get(
                url,
                headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
                timeout=TIMEOUT_SECONDS,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                written = 0
                with open(self._part, "wb") as fh:
                    for chunk in resp.iter_content(CHUNK_BYTES):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
            if written != expected:
                raise ValueError(f"size mismatch: got {written} bytes, expected {expected}")
            with open(self._part, "rb") as fh:
                if fh.read(2) != b"MZ":
                    raise ValueError("downloaded file is not a Windows executable")
            os.replace(self._part, self._new)
        except Exception:
            _remove(self._part)
            raise

    def _apply(self, latest: str) -> bool:
        os.replace(self._exe, self._old)
        try:
            os.replace(self._new, self._exe)
        except Exception:
            os.replace(self._old, self._exe)
            raise
        self._ready_version = None
        logger.info("Installed update v%s", latest)
        self._on_status(f"Updating to v{latest} — restarting...")
        env = dict(os.environ)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            subprocess.Popen([self._exe], env=env, close_fds=True)
        except Exception as exc:
            logger.warning("Restart after update failed: %s: %s", type(exc).__name__, exc)
            self._on_status(f"Updated to v{latest} — close and reopen the app to use it")
            return True
        self._quit_app()
        return True
