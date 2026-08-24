import pytest

import tomorrow_strategy as ts


@pytest.mark.parametrize(
    "close,c20,c60,slope,down_vol,macd_neg,expected",
    [
        (110, 105, 100, 1, False, False, "A"),
        (102, 105, 100, 0, False, False, "B1"),
        (102, 105, 100, -1, True, False, "B2"),
        (99, 105, 100, -1, False, True, "C"),
    ],
)
def test_existing_grade_definitions_are_preserved(
    close, c20, c60, slope, down_vol, macd_neg, expected
):
    grade, _, _ = ts._classify_grade(close, c20, c60, slope, down_vol, macd_neg)
    assert grade == expected

