"""Federal payroll tax parameters by year.

Every number here comes from an IRS / SSA publication. Update this file each
January (Pub. 15-T, Rev. Proc. inflation adjustments, SSA wage base).

Amounts are integer cents. Bracket lists are (taxable income where the rate starts, rate).

The percentage-method withholding tables in Pub. 15-T (Worksheet 1A, annual
"automated payroll" tables for 2020+ Forms W-4) are *derived* from the regular
tax brackets:
  * Standard tables start at  standard_deduction - line_1g amount
  * Step 2 checkbox tables start at standard_deduction / 2 and use half-width brackets.
For 2026 this reproduces the published MFJ table (0% to $19,300, 10% to $44,100,
12% to $120,100, 24% from $230,700; Step 2 table starting at $16,100).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

FILING_STATUSES = {
    "single": "Single or Married filing separately",
    "mfj": "Married filing jointly (or Qualifying surviving spouse)",
    "hoh": "Head of household",
}


@dataclass(frozen=True)
class YearParams:
    year: int
    ss_rate: Decimal
    ss_wage_base: int
    medicare_rate: Decimal
    addl_medicare_rate: Decimal
    addl_medicare_threshold: int
    futa_rate: Decimal  # net rate after the 5.4% state credit
    futa_wage_base: int
    standard_deduction: dict[str, int]
    line_1g: dict[str, int]  # Pub. 15-T Worksheet 1A, line 1g
    brackets: dict[str, list[tuple[int, Decimal]]]
    sources: list[str] = field(default_factory=list)


def _b(rows):
    return [(int(start * 100), Decimal(rate)) for start, rate in rows]


TAX_YEARS: dict[int, YearParams] = {
    2026: YearParams(
        year=2026,
        ss_rate=Decimal("0.062"),
        ss_wage_base=184_500_00,
        medicare_rate=Decimal("0.0145"),
        addl_medicare_rate=Decimal("0.009"),
        addl_medicare_threshold=200_000_00,
        futa_rate=Decimal("0.006"),
        futa_wage_base=7_000_00,
        standard_deduction={"single": 16_100_00, "mfj": 32_200_00, "hoh": 24_150_00},
        line_1g={"single": 8_600_00, "mfj": 12_900_00, "hoh": 8_600_00},
        brackets={
            "single": _b([(0, "0.10"), (12_400, "0.12"), (50_400, "0.22"), (105_700, "0.24"),
                          (201_775, "0.32"), (256_225, "0.35"), (640_600, "0.37")]),
            "mfj": _b([(0, "0.10"), (24_800, "0.12"), (100_800, "0.22"), (211_400, "0.24"),
                       (403_550, "0.32"), (512_450, "0.35"), (768_700, "0.37")]),
            "hoh": _b([(0, "0.10"), (17_700, "0.12"), (67_450, "0.22"), (105_700, "0.24"),
                       (201_750, "0.32"), (256_200, "0.35"), (640_600, "0.37")]),
        },
        sources=[
            "Rev. Proc. 2025-32 (2026 brackets and standard deduction, as amended by P.L. 119-21)",
            "IRS Pub. 15-T (2026), Worksheet 1A line 1g: $12,900 MFJ / $8,600 otherwise",
            "SSA: 2026 Social Security wage base $184,500",
            "IRS Pub. 15 (2026): Medicare 1.45%, Additional Medicare 0.9% over $200,000; FUTA 0.6% on first $7,000",
        ],
    ),
}


def params_for(year: int) -> YearParams:
    try:
        return TAX_YEARS[year]
    except KeyError:
        raise LookupError(
            f"No payroll tax tables for {year}. Add them to timeentry/tax/tables.py "
            f"from that year's IRS Pub. 15-T before running payroll."
        ) from None


@dataclass(frozen=True)
class WithholdingRow:
    start: int          # annual adjusted wage amount where the row starts (cents)
    base_tax: int       # tentative tax at `start` (cents)
    rate: Decimal


def withholding_table(year: int, status: str, step2_checked: bool) -> list[WithholdingRow]:
    """Build the Pub. 15-T annual percentage-method table for a filing status."""
    p = params_for(year)
    brackets = p.brackets[status]
    if step2_checked:
        offset = p.standard_deduction[status] // 2
        brackets = [(start // 2, rate) for start, rate in brackets]
    else:
        offset = p.standard_deduction[status] - p.line_1g[status]
    rows = [WithholdingRow(0, 0, Decimal("0"))]
    base = Decimal(0)
    for i, (start, rate) in enumerate(brackets):
        if i > 0:
            prev_start, prev_rate = brackets[i - 1]
            base += Decimal(start - prev_start) * prev_rate
        rows.append(WithholdingRow(offset + start, int(base), rate))
    return rows
