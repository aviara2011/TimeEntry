import pytest

from timeentry.money import amount_for_minutes, fmt_money, parse_hours, parse_money


def test_parse_money():
    assert parse_money("1,234.56") == 123456
    assert parse_money("$10") == 1000
    assert parse_money("") == 0
    assert parse_money("0.005") == 1  # half up
    with pytest.raises(ValueError):
        parse_money("abc")


def test_parse_hours():
    assert parse_hours("1.5") == 90
    assert parse_hours("1:30") == 90
    assert parse_hours("45m") == 45
    assert parse_hours("0.1") == 6
    with pytest.raises(ValueError):
        parse_hours("")


def test_amounts():
    assert amount_for_minutes(90, 15000) == 22500
    assert amount_for_minutes(10, 10000) == 1667
    assert fmt_money(-123456) == "-$1,234.56"
