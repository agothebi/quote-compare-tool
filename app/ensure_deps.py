"""First-run setup, used by start.command and start.bat. Standard library only (runs before anything is installed).

Installs requirements.txt into the virtual environment it runs in, and again only when that file
changes (a hash stamp in the venv), so every later start is instant.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"


def main() -> int:
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is needed; this is {sys.version.split()[0]}.")
        return 1
    stamp = Path(sys.prefix) / ".requirements.sha256"
    digest = hashlib.sha256(REQ.read_bytes()).hexdigest()
    if stamp.exists() and stamp.read_text().strip() == digest:
        return 0
    print("Installing what the app needs (first start only; this takes a few minutes)...", flush=True)
    for cmd in ([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"],
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQ)]):
        if subprocess.call(cmd) != 0:
            print("\nThe install did not finish. Check the internet connection and start the app again.")
            return 1
    stamp.write_text(digest)
    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
