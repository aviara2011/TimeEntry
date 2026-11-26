"""Money and hours helpers. All money is stored as integer cents; time as integer minutes."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def round_cents(value: Decimal) -> int:
    """Round a Decimal amount of cents to a whole cent (half up)."""
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_money(text: str | None) -> int:
    """Parse '1,234.56' or '$1234' into cents. Blank -> 0."""
    if text is None:
        return 0
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0
    try:
        return round_cents(Decimal(cleaned) * 100)
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid amount: {text!r}") from exc


def fmt_money(cents: int | None) -> str:
    if cents is None:
        cents = 0
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def cents_to_str(cents: int | None) -> str:
    """Plain decimal string for form inputs ('1234.56')."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_hours(text: str | None) -> int:
    """Parse '1.5', '1:30' or '90m' into minutes."""
    if text is None or not str(text).strip():
        raise ValueError("Hours are required")
    s = str(text).strip().lower()
    try:
        if ":" in s:
            h, m = s.split(":", 1)
            minutes = int(h or 0) * 60 + int(m or 0)
        elif s.endswith("m"):
            minutes = int(s[:-1])
        else:
            minutes = round_cents(Decimal(s.rstrip("h")) * 60)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"Not a valid time: {text!r}") from exc
    if minutes < 0:
        raise ValueError("Time cannot be negative")
    return minutes


def fmt_hours(minutes: int | None) -> str:
    return f"{Decimal(minutes or 0) / 60:.2f}"


def amount_for_minutes(minutes: int, rate_cents: int) -> int:
    """Billable amount for `minutes` at an hourly rate in cents."""
    return round_cents(Decimal(minutes) * Decimal(rate_cents) / 60)


def parse_rate(text: str | None) -> Decimal:
    """Parse a percentage like '2.7' or '2.7%' into a fraction Decimal('0.027')."""
    if text is None or not str(text).strip():
        return Decimal("0")
    return Decimal(str(text).replace("%", "").strip()) / 100
