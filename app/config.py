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
