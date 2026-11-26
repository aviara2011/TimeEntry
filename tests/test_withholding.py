from timeentry.tax.tables import withholding_table
from timeentry.tax.withholding import W4, federal_withholding


def starts(status, step2):
    return [r.start for r in withholding_table(2026, status, step2)]


def test_tables_match_published_2026_pub15t_mfj():
    # Published 2026 Pub. 15-T annual MFJ standard table: 0 to $19,300; 10% to $44,100;
    # 12% to $120,100; 24% bracket starts at $230,700. Step 2 table starts at $16,100.
    s = starts("mfj", False)
    assert s[1:5] == [19_300_00, 44_100_00, 120_100_00, 230_700_00]
    assert starts("mfj", True)[1] == 16_100_00


def test_mfj_monthly():
    # $10,000/month: 120,000 - 12,900 = 107,100; 2,480 + (107,100-44,100)*12% = 10,040; /12
    assert federal_withholding(1_000_000, 12, W4(filing_status="mfj"), 2026) == 83667


def test_single_biweekly():
    # $3,000 biweekly: 78,000 - 8,600 = 69,400; 5,800 + (69,400-57,900)*22% = 8,330; /26
    assert federal_withholding(300_000, 26, W4(filing_status="single"), 2026) == 32038


def test_step3_and_4c():
    base = federal_withholding(1_000_000, 12, W4(filing_status="mfj"), 2026)
    w = W4(filing_status="mfj", step3_credits=240_000, step4c_extra=5_000)
    assert federal_withholding(1_000_000, 12, w, 2026) == base - 20_000 + 5_000


def test_low_wages_and_exempt():
    assert federal_withholding(100_000, 12, W4(filing_status="mfj"), 2026) == 0
    assert federal_withholding(1_000_000, 12, W4(exempt=True), 2026) == 0


def test_step2_withholds_more():
    plain = federal_withholding(800_000, 12, W4(filing_status="mfj"), 2026)
    step2 = federal_withholding(800_000, 12, W4(filing_status="mfj", step2_checked=True), 2026)
    assert step2 > plain
