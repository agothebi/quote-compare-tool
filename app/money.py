"""Parse printed money strings into Decimals. Used by the checks only, never for displayed values.

    "$1,447,000.00" -> 1447000.00    "($145.00)" -> -145.00    "-12.00" -> -12.00
    "$ 308.00" -> 308.00             "Included" -> None (not an amount)
"""
from __future__ import annotations

import re
import sys
from decimal import Decimal, InvalidOperation

# Words printed in a premium column that mean "no separate charge".
NO_CHARGE_WORDS = {"included", "incl", "incl.", "waived", "no charge", "none", "n/a", "-", "--"}

_MONEY = re.compile(r"^(?P<open>\()?\s*(?P<sign>[-−])?\s*\$?\s*(?P<sign2>[-−])?\s*"
                    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?\s*(?P<close>\))?$")


def parse(text: str | None) -> Decimal | None:
    """The amount in a printed money string, or None if it is not exactly one amount."""
    if text is None:
        return None
    s = text.strip().replace("−", "-")
    m = _MONEY.match(s)
    if not m or bool(m.group("open")) != bool(m.group("close")):
        return None
    try:
        value = Decimal(m.group("num").replace(",", "") + (m.group("dec") or ""))
    except InvalidOperation:
        return None
    negative = bool(m.group("open")) or bool(m.group("sign")) or bool(m.group("sign2"))
    return -value if negative else value


def is_no_charge(text: str | None) -> bool:
    return (text or "").strip().lower() in NO_CHARGE_WORDS


def fmt(value: Decimal) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(repr(arg), "->", parse(arg))
