"""Working calendar: Monday to Friday, 8 hours, minus the public holidays of the organisation's region.

The one region built in so far is "tas-south" (Tasmania, south); "none" has no holidays.

Holidays are computed for any year, so plans that cross a year end stay right. Southern Tasmania observes Royal Hobart
Regatta and the Royal Hobart Show; the north's Recreation Day is not included. Extra non-working days (site shutdowns,
the Christmas close-down) come from the plan spec.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

HOURS_PER_DAY = 8


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(d: date) -> date:
    """Weekend holidays move to the following Monday."""
    return d + timedelta(days={5: 2, 6: 1}.get(d.weekday(), 0))


@lru_cache(maxsize=64)
def tas_south_holidays(year: int) -> dict[date, str]:
    easter = _easter(year)
    christmas, boxing = date(year, 12, 25), date(year, 12, 26)
    if christmas.weekday() == 5:      # Sat: Christmas Mon 27, Boxing Tue 28
        christmas_obs, boxing_obs = date(year, 12, 27), date(year, 12, 28)
    elif christmas.weekday() == 6:    # Sun: Christmas Tue 27 (Boxing Mon 26 stays)
        christmas_obs, boxing_obs = date(year, 12, 27), boxing
    else:
        christmas_obs, boxing_obs = christmas, _observed(boxing) if boxing.weekday() >= 5 else boxing
    fourth_sat_oct = _nth_weekday(year, 10, 5, 4)
    days = {
        _observed(date(year, 1, 1)): "New Year's Day",
        _observed(date(year, 1, 26)): "Australia Day",
        _nth_weekday(year, 2, 0, 2): "Royal Hobart Regatta",
        _nth_weekday(year, 3, 0, 2): "Eight Hours Day",
        easter - timedelta(days=2): "Good Friday",
        easter + timedelta(days=1): "Easter Monday",
        date(year, 4, 25): "Anzac Day",
        _nth_weekday(year, 6, 0, 2): "King's Birthday",
        fourth_sat_oct - timedelta(days=2): "Royal Hobart Show",
        christmas_obs: "Christmas Day",
        boxing_obs: "Boxing Day",
    }
    return {d: n for d, n in days.items() if d.weekday() < 5}


class WorkCalendar:
    """Maps working-day indices (0 = the project start's first working day) to dates and back."""

    def __init__(self, start: date, extra_non_working: dict[date, str] | None = None, region: str = "none"):
        self.region = region
        self.extra = dict(extra_non_working or {})
        self.start = self._next_working(start)
        self._days: list[date] = [self.start]

    def holiday(self, d: date) -> str | None:
        if d in self.extra:
            return self.extra[d]
        return tas_south_holidays(d.year).get(d) if self.region == "tas-south" else None

    def is_working(self, d: date) -> bool:
        return d.weekday() < 5 and self.holiday(d) is None

    def _next_working(self, d: date) -> date:
        while not self.is_working(d):
            d += timedelta(days=1)
        return d

    def date_of(self, index: int) -> date:
        """The date of working day `index` (can be negative for dates before the start)."""
        if index < 0:
            d, n = self.start, 0
            while n > index:
                d -= timedelta(days=1)
                if self.is_working(d):
                    n -= 1
            return d
        while len(self._days) <= index:
            self._days.append(self._next_working(self._days[-1] + timedelta(days=1)))
        return self._days[index]

    def index_of(self, d: date) -> int:
        """Working-day index of `d`, or of the next working day if `d` isn't one."""
        d = self._next_working(d) if d >= self.start else d
        if d < self.start:
            n, cur = 0, self.start
            while cur > d:
                cur -= timedelta(days=1)
                if self.is_working(cur):
                    n -= 1
            return n
        lo = 0
        while self.date_of(lo) < d:
            lo += 1
        return lo

    def non_working_between(self, first: date, last: date) -> dict[date, str]:
        """Holidays and extra non-working weekdays in [first, last] (for calendars and shading)."""
        out, d = {}, first
        while d <= last:
            if d.weekday() < 5 and (name := self.holiday(d)):
                out[d] = name
            d += timedelta(days=1)
        return out
