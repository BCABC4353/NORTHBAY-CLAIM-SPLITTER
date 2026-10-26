"""NorthBay Claim PDF Splitter — application entrypoint."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _try_file_handler(directory: str) -> logging.Handler | None:
    """Try to create a FileHandler under ``directory``. Return None on failure.

    Bundled-exe deployments hit corporate environments where APPDATA is unset,
    the home directory is read-only, or the temp dir is on a quota-locked
    network share. Any of those would make ``os.makedirs`` or
    ``FileHandler.__init__`` raise — and because ``console=False`` swallows
    stderr, the operator would see the GUI fail to launch with no diagnostic.
    """
    try:
        os.makedirs(directory, exist_ok=True)
        log_path = os.path.join(directory, "splitter.log")
        return logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        return None


def _setup_logging() -> None:
    handlers: list[logging.Handler] = []

    appdata = os.environ.get("APPDATA")
    if appdata:
        handler = _try_file_handler(os.path.join(appdata, "NorthBay Claim Splitter"))
        if handler is not None:
            handlers.append(handler)

    if not handlers:
        handler = _try_file_handler(
            os.path.join(os.path.expanduser("~"), "NorthBay Claim Splitter"),
        )
        if handler is not None:
            handlers.append(handler)

    if not handlers:
        handler = _try_file_handler(
            os.path.join(tempfile.gettempdir(), "NorthBay Claim Splitter"),
        )
        if handler is not None:
            handlers.append(handler)

    # StreamHandler is best-effort: in a windowed (console=False) bundle
    # sys.stderr can be None. Add it only if it exists so we don't crash here.
    if sys.stderr is not None:
        with contextlib.suppress(OSError, ValueError):
            handlers.append(logging.StreamHandler(sys.stderr))

    if not handlers:
        # Last resort: silence logging rather than let basicConfig raise.
        handlers.append(logging.NullHandler())

    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=handlers)


def main() -> int:
    try:
        _setup_logging()
    except Exception:
        # Never let logging setup prevent the GUI from launching.
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])

    from splitter_gui import SplitterApp

    app = SplitterApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
