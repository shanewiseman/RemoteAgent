from __future__ import annotations

from datetime import UTC, datetime

import pytest

from remoteagent_cron.scheduling import (
    CronValidationError,
    next_occurrence,
    validate_cron_expression,
    validate_timezone,
)


@pytest.mark.parametrize(
    "expression",
    [
        "*/5 * * * *",
        "1,2,10-20/2 0-12/3 1,15 jan,mar mon-fri",
        "0 0 1 * mon",
        "0 0 * * 0,7",
    ],
)
def test_strict_five_field_grammar_accepts_supported_forms(expression: str) -> None:
    assert validate_cron_expression(expression) == expression


@pytest.mark.parametrize(
    "expression",
    [
        "@daily",
        "0 0 * * * *",
        "H * * * *",
        "R * * * *",
        "0 0 L * *",
        "0 0 31 2 *",
        "0 0 * * mon#2",
        "0 0 * * ?",
    ],
)
def test_strict_grammar_rejects_extensions_and_impossible_dates(expression: str) -> None:
    with pytest.raises(CronValidationError):
        validate_cron_expression(expression)


def test_day_of_month_and_weekday_use_conventional_or_semantics() -> None:
    result = next_occurrence(
        "0 0 1 * mon",
        "UTC",
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert result == datetime(2026, 9, 7, tzinfo=UTC)


def test_dst_gap_is_skipped_and_fold_runs_only_first_copy() -> None:
    gap = next_occurrence(
        "30 2 * * *",
        "America/New_York",
        datetime(2026, 3, 8, 0, tzinfo=UTC),
    )
    assert gap == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)

    first_fold = next_occurrence(
        "30 1 * * *",
        "America/New_York",
        datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
    )
    assert first_fold == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    after_first_fold = next_occurrence(
        "30 1 * * *",
        "America/New_York",
        datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
    )
    assert after_first_fold == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_multi_hour_dst_gap_does_not_exhaust_candidate_search() -> None:
    result = next_occurrence(
        "* 1-2 * * *",
        "Antarctica/Troll",
        datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
    )
    assert result > datetime(2026, 3, 29, 0, 0, tzinfo=UTC)


def test_timezone_validation_is_fail_closed() -> None:
    assert validate_timezone("UTC") == "UTC"
    with pytest.raises(CronValidationError, match="unknown timezone"):
        validate_timezone("Mars/Olympus_Mons")
