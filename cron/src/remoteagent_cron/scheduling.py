from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter


class CronValidationError(ValueError):
    pass


_NAMES: dict[int, frozenset[str]] = {
    3: frozenset(
        {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}
    ),
    4: frozenset({"sun", "mon", "tue", "wed", "thu", "fri", "sat"}),
}


def _validate_atom(atom: str, field_index: int) -> None:
    if atom == "*" or atom.isdigit():
        return
    if atom in _NAMES.get(field_index, frozenset()):
        return
    raise CronValidationError(f"unsupported token {atom!r} in cron field {field_index + 1}")


def _validate_component(component: str, field_index: int) -> None:
    if not component:
        raise CronValidationError("cron fields cannot contain empty list items")
    base, separator, step = component.partition("/")
    if separator:
        if not step.isdigit() or int(step) < 1 or "/" in step:
            raise CronValidationError("cron steps must be positive integers")
    if base.count("-") > 1:
        raise CronValidationError("cron ranges must contain two endpoints")
    if "-" in base:
        start, end = base.split("-", 1)
        _validate_atom(start, field_index)
        _validate_atom(end, field_index)
    else:
        _validate_atom(base, field_index)


def validate_cron_expression(expression: str) -> str:
    """Validate and normalize the supported, strict five-field cron grammar."""

    normalized = " ".join(expression.strip().lower().split())
    fields = normalized.split(" ")
    if len(fields) != 5:
        raise CronValidationError("cron_expression must contain exactly five fields")
    if expression.lstrip().startswith("@"):
        raise CronValidationError("cron macros are not supported")
    for field_index, field in enumerate(fields):
        for component in field.split(","):
            _validate_component(component, field_index)
    try:
        if not croniter.is_valid(normalized):
            raise CronValidationError("invalid cron expression")
        # Force croniter to prove the expression has a real calendar occurrence.
        croniter(
            normalized,
            datetime(2024, 1, 1),
            day_or=True,
            max_years_between_matches=50,
        ).get_next(datetime)
    except (CroniterBadCronError, CroniterBadDateError, ValueError, KeyError) as exc:
        if isinstance(exc, CronValidationError):
            raise
        raise CronValidationError("cron expression has no valid calendar occurrence") from exc
    return normalized


def validate_timezone(name: str) -> str:
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronValidationError(f"unknown timezone: {name}") from exc
    return name


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_occurrence(expression: str, timezone_name: str, after: datetime) -> datetime:
    """Return the next real wall-clock occurrence as UTC.

    Croniter runs against a naive local wall clock. We localize each candidate
    explicitly: nonexistent DST minutes are discarded and an ambiguous minute
    always maps to fold zero, so a fall-back minute can fire only once.
    """

    expression = validate_cron_expression(expression)
    timezone_name = validate_timezone(timezone_name)
    zone = ZoneInfo(timezone_name)
    after_utc = _as_utc(after)
    base = after_utc.astimezone(zone).replace(tzinfo=None)

    # Real timezone jumps are normally at most a day, but retain ample room for
    # historical date-line changes combined with sparse annual expressions.
    for _ in range(4096):
        try:
            candidate = croniter(
                expression,
                base,
                day_or=True,
                max_years_between_matches=50,
            ).get_next(datetime)
        except (CroniterBadCronError, CroniterBadDateError, ValueError) as exc:
            raise CronValidationError("cron expression has no future occurrence") from exc
        candidate = candidate.replace(tzinfo=None)
        first_fold = candidate.replace(tzinfo=zone, fold=0)
        round_trip = first_fold.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip != candidate:
            # The requested local minute is inside a spring-forward gap.
            base = candidate
            continue
        result = first_fold.astimezone(UTC)
        if result > after_utc:
            return result
        # We may be in the second copy of an ambiguous wall-clock hour. Advancing
        # the naive base past this candidate deliberately suppresses fold one.
        base = candidate
    raise CronValidationError("unable to resolve a future cron occurrence")
