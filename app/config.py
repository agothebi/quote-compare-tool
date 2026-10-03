"""Paths and config loading shared by all pipeline steps."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
FIXTURES_DIR = ROOT / "fixtures"
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"


class ConfigError(RuntimeError):
    """A config file is missing or not valid YAML; the message says which file and where."""


_yaml_cache: dict = {}


def load_yaml(path: Path) -> dict:
    """Parsed YAML, re-read only when the file changes. Returns a copy: callers may modify it."""
    import copy
    try:
        stamp = path.stat().st_mtime_ns
    except FileNotFoundError:
        raise ConfigError(f"{path} is missing")
    hit = _yaml_cache.get(path)
    if not hit or hit[0] != stamp:
        try:
            with open(path, encoding="utf-8") as f:
                hit = (stamp, yaml.safe_load(f))
        except yaml.YAMLError as e:
            raise ConfigError(f"{path.name} has a mistake: {e}") from e
        _yaml_cache[path] = hit
    return copy.deepcopy(hit[1])


def settings() -> dict:
    return load_yaml(CONFIG_DIR / "settings.yaml")


def lines_config() -> dict:
    return load_yaml(CONFIG_DIR / "lines.yaml")


# ---------------------------------------------------------------- the proposal's email text
# The broker edits email.txt in the app folder (made on the first start; updates never touch it).
# {placeholders} are filled in by the proposal page; the README lists them and repeats this default.
EMAIL_FILE = ROOT / "email.txt"
DEFAULT_EMAIL = """Hi,

I compared the quotes we received for you. Here's what I recommend:

{recommendations}

{total}

{notes}

The full side-by-side comparison is attached.

{signature}
"""
_email_cache: dict = {}


def email_template(path: Path | None = None) -> str:
    """email.txt's text, or the default when it is missing, empty, too long or unreadable."""
    path = path or EMAIL_FILE
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key not in _email_cache:
            _email_cache.clear()
            _email_cache[key] = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        text = _email_cache[key]
    except (OSError, UnicodeDecodeError):
        return DEFAULT_EMAIL
    return text if text.strip() and len(text) <= 20_000 else DEFAULT_EMAIL


def ensure_email_file(path: Path | None = None) -> None:
    """Writes the default email.txt when there is none, so the broker has a file to edit."""
    path = path or EMAIL_FILE
    try:
        if not path.exists():
            path.write_text(DEFAULT_EMAIL, encoding="utf-8")
    except OSError:
        pass  # the proposal page then uses the default text


def load_env(path: Path = ROOT / ".env") -> None:
    """Load KEY=value lines from .env into os.environ (existing variables win)."""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Windows Notepad's BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)
