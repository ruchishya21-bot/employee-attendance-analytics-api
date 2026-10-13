from datetime import datetime

import pytest
from fastapi import HTTPException

from app.main import (
    EmployeeIn,
    attendance_date_for,
    compute_derived,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    epoch_ms_to_utc,
    shift_bounds,
)


def test_late_arrival_at_exactly_ten_minute_grace_boundary():
    # 09:40 IST is exactly 10 minutes after a 09:30 shift start.
    punch_in = datetime(2026, 10, 12, 4, 10, 0)

    assert compute_late_minutes(
        punch_in, "09:30", "18:30"
    ) == 0


def test_late_arrival_one_second_after_grace_boundary():
    # 09:40:01 IST is 10 minutes and 1 second after shift start.
    punch_in = datetime(2026, 10, 12, 4, 10, 1)

    assert compute_late_minutes(
        punch_in, "09:30", "18:30"
    ) == 10


def test_work_hours_are_rounded_to_two_decimal_places():
    punch_in = datetime(2026, 10, 12, 4, 0, 0)
    punch_out = datetime(2026, 10, 12, 8, 29, 0)

    assert compute_work_hours(punch_in, punch_out) == 4.48


def test_overtime_is_zero_before_thirty_minute_threshold():
    punch_in = datetime(2026, 10, 12, 4, 0, 0)
    punch_out = datetime(2026, 10, 12, 13, 29, 0)

    assert compute_overtime(
        punch_out, "18:30", punch_in, "09:30"
    ) == 0


def test_overtime_counts_at_thirty_minute_threshold():
    punch_in = datetime(2026, 10, 12, 4, 0, 0)
    punch_out = datetime(2026, 10, 12, 13, 30, 0)

    assert compute_overtime(
        punch_out, "18:30", punch_in, "09:30"
    ) == 30


def test_overnight_shift_bounds():
    # 22:00 IST on October 12 is 16:30 UTC.
    # 06:00 IST on October 13 is 00:30 UTC.
    punch_in = datetime(2026, 10, 12, 16, 30, 0)

    start, end = shift_bounds(
        punch_in, "22:00", "06:00"
    )

    assert start == datetime(2026, 10, 12, 16, 30, 0)
    assert end == datetime(2026, 10, 13, 0, 30, 0)


def test_early_morning_overnight_punch_uses_previous_attendance_date():
    # 01:30 IST on October 13 belongs to the shift starting October 12.
    punch_in = datetime(2026, 10, 12, 20, 0, 0)

    assert attendance_date_for(
        punch_in, "22:00", "06:00"
    ) == "2026-10-12"


def test_half_day_when_rounded_work_hours_are_below_four_and_half():
    employee = {
        "shift_start": "09:30",
        "shift_end": "18:30",
    }

    punch_in = datetime(2026, 10, 12, 4, 0, 0)
    punch_out = datetime(2026, 10, 12, 8, 29, 0)

    result = compute_derived(
        employee, "PRESENT", punch_in, punch_out
    )

    assert result["work_hours"] == 4.48
    assert result["half_day"] is True


def test_exactly_four_and_half_work_hours_is_not_half_day():
    employee = {
        "shift_start": "09:30",
        "shift_end": "18:30",
    }

    punch_in = datetime(2026, 10, 12, 4, 0, 0)
    punch_out = datetime(2026, 10, 12, 8, 30, 0)

    result = compute_derived(
        employee, "PRESENT", punch_in, punch_out
    )

    assert result["work_hours"] == 4.5
    assert result["half_day"] is False


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        1791784800,       # Seconds instead of milliseconds
        1791784800000.0,  # Float instead of integer
        "1791784800000",  # String instead of integer
        True,             # Boolean instead of integer
    ],
)
def test_invalid_timestamps_are_rejected(invalid_timestamp):
    with pytest.raises(HTTPException) as exc:
        epoch_ms_to_utc(invalid_timestamp)

    assert exc.value.status_code == 422


def test_employee_code_format_is_validated():
    with pytest.raises(ValueError):
        EmployeeIn(
            emp_code="EMP123",
            name="Test Employee",
            email="test@example.com",
            department="Engineering",
            joined_on="2026-01-15",
        )