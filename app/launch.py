"""Starts the app for the broker: checks, server, browser. Used by start.command and start.bat.

    python -m app.launch [--no-browser]

- Tesseract missing: a warning (PDFs with a text layer still work; scans need it).
- API key missing: a warning (the app opens; quotes can't be read until the key is in .env).
- The app already running: opens the browser on it instead of starting a second copy.
- The port taken by another program: uses the next free port.
"""
from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from app.config import ROOT, load_env, settings

LINE = "-" * 64


def _port_free(host: str, port: int) -> bool:
    """Nothing listens there. (A test bind proves nothing on Windows: with SO_REUSEADDR it succeeds
    even while another program is listening on the port.)"""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def _is_our_app(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/comparisons", timeout=2) as r:
            return "comparisons" in json.loads(r.read())
    except Exception:
        return False


def _running_build(url: str) -> str | None:
    """The build id of the copy running at url; None for a copy from before build ids existed."""
    try:
        with urllib.request.urlopen(f"{url}/api/version", timeout=2) as r:
            return json.loads(r.read())["build"]
    except Exception:
        return None


def _stop(url: str, host: str, port: int) -> bool:
    req = urllib.request.Request(f"{url}/api/shutdown", method="POST", headers={"X-Quote-Compare": "1"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        return False
    for i in range(1000):  # up to 100 s: a file being read finishes first, then the port frees
        if _port_free(host, port):
            return True
        if i == 30:
            print("Waiting for the file it is reading to finish...")
        time.sleep(0.1)
    return False


def check_tesseract() -> str | None:
    from app.read import tesseract_cmd
    cmd = tesseract_cmd()
    if cmd:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = cmd
            pytesseract.get_tesseract_version()
            return None
        except Exception:
            pass
    how = ("Install it with:  brew install tesseract" if platform.system() == "Darwin" else
           "Install it from the UB Mannheim installer (github.com/UB-Mannheim/tesseract/wiki)"
           if platform.system() == "Windows" else "Install the tesseract-ocr package")
    return ("Tesseract (the program that reads scanned pages) was not found.\n"
            f"  Quotes that are scans or photos can't be read until it is installed.\n  {how}")


def save_key(env: str, key: str, path=None) -> None:
    """Write `env=key` into .env: the line is replaced when there is one, otherwise added. The file
    starts from .env.example, so it keeps the notes on which key goes where."""
    import re
    path = path or ROOT / ".env"
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
    else:
        example = ROOT / ".env.example"
        text = example.read_text(encoding="utf-8") if example.exists() else ""
    line = f"{env}={key}"
    pattern = re.compile(rf"^{re.escape(env)}=.*$", re.M)
    text = pattern.sub(lambda m: line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def ask_for_key(env: str) -> bool:
    """First start without a key: ask for it in this window and save it. Only in a real window."""
    import os
    if not sys.stdin or not sys.stdin.isatty():
        return False
    print(LINE)
    print("  Paste the API key you were given, then press Return.")
    print("  (Press Return without a key to skip; quotes can't be read until it is added.)")
    print(LINE)
    try:
        key = input("  API key: ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not key:
        return False
    save_key(env, key)
    os.environ[env] = key
    print("  Saved. You won't be asked again.\n")
    return True


def check_key() -> str | None:
    load_env()
    import os
    from app import llm
    try:
        s = llm.llm_settings()
    except llm.LLMError as e:  # a model name in .env or settings.yaml that isn't known
        return f"{e}\n  Quotes can't be read until this is fixed (see the README), then restart."
    env = s["api_key_env"]
    if os.environ.get(env) or ask_for_key(env):
        return None
    return (f"No API key found ({env}). The app opens, but quotes can't be read yet.\n"
            f"  Put the key in {ROOT / '.env'} as  {env}=...  (see .env.example), then restart.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    from app.config import ConfigError, lines_config
    try:
        cfg = settings()["app"]
        lines_config()
    except (ConfigError, KeyError, TypeError) as e:
        print(f"The app's settings could not be read: {e}\nUndo the last change to the config folder, then start again.")
        return 1
    from app.config import ensure_email_file
    ensure_email_file()  # email.txt: the proposal's email text, the broker's to edit (also when already running)
    host, port = cfg["host"], int(cfg["port"])
    if host not in ("127.0.0.1", "localhost"):
        print(f"app.host in config/settings.yaml must be 127.0.0.1, not {host}.")
        return 1

    url = f"http://{host}:{port}"
    from app import version
    if not _port_free(host, port) and _is_our_app(url):
        running = _running_build(url)
        if running == version.RUNNING:
            print(f"Quote Compare is already running: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print("An older copy of Quote Compare is running. Restarting it with the updated version...")
        if running is None or not _stop(url, host, port):
            print("The older copy could not be stopped. Close its window (or press Ctrl+C in it), then start again.")
            return 1
    if not _port_free(host, port):
        port = next((p for p in range(port + 1, port + 20) if _port_free(host, p)), None)
        if port is None:
            print("No free port found between 8001 and 8020. Close other programs and try again.")
            return 1
        url = f"http://{host}:{port}"

    warnings = [w for w in (check_tesseract(), check_key()) if w]
    print(LINE)
    print(f"  Quote Compare is starting at {url}")
    print("  Keep this window open while you use it. Close it (or press Ctrl+C) to stop.")
    print(LINE)
    for w in warnings:
        print("! " + w + "\n")

    import uvicorn

    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    import app.main as main_module
    main_module.SHUTDOWN_HOOK = lambda: setattr(server, "should_exit", True)

    def open_when_ready() -> None:
        for _ in range(200):
            if server.started:
                if not args.no_browser:
                    webbrowser.open(url)
                return
            time.sleep(0.05)

    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    print("Quote Compare stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
