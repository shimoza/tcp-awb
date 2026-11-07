"""Pooled Reserved pricing of one billing item: how many units for many machines that each run part time (KB-NPRJ).

Reserved and Reserved Upfront are a monthly usage contingent per flavor type, not per instance: one or several
instances use it, at the same time too; it is billed when it is not used up (KB-B2JD, KB-UD2I). The billing item
is the flavor plus the OS, so Windows and Linux of one flavor are two items and are never pooled together (KB-SFB4).
The contingent of one unit in a month is days x 24 h and unused hours expire; use beyond it runs Open Elastic.

The method (KB-NPRJ): count the pooled hours H of all instances of the item per month. The unit count n is booked for
the whole term, so it is chosen once against every month of the term, never as the best n of one month. In a month
of M hours n units cover n x M hours and the rest max(0, H - n x M) runs Open Elastic at the PAYG rate. One more unit
pays off when the rest of a month is above the threshold R / PAYG.

The month model is the real calendar months from a start date (the term starts at 0:00 on the order day, the first
and the last month are pro rata, KB-LWOH) or 730 h for each month when no start is given. The monthly rate of an
Upfront term (RU12, RU24, RU36) is the upfront total divided by the months of the term (KB-DXYK), so it is used the
same way; the whole upfront total is invoiced at the start. A rate of 0 means the term is not offered (KB-COBA).

Every amount is exact decimal arithmetic (awb/calc.py), each money figure of a month rounded half up to the cent.
No I/O: `awb price pool` and `awb pricebook` (mode: pool) fetch the rates and call `pool` here.
"""
from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass
from decimal import Decimal

from awb import calc

AVERAGE_HOURS = Decimal(730)
TERM_MONTHS = {"R12": 12, "R24": 24, "R36": 36, "RU12": 12, "RU24": 24, "RU36": 36}
UPFRONT = ("RU12", "RU24", "RU36")
TERMS = tuple(TERM_MONTHS)
MAX_UNITS = 100000


class PoolError(Exception):
    """A pool that cannot be computed. The message says why."""


class NotOffered(PoolError):
    """The price API carries the rate 0 for the term: the term is not offered (KB-DXYK, KB-COBA)."""


@dataclass(frozen=True)
class Month:
    label: str                 # 2027-02 or "month 1" in the 730 h model
    hours: Decimal             # the contingent of one unit: days in the term x 24 h or 730
    share: Decimal             # the part of the calendar month inside the term (1 for a whole month)
    pooled: Decimal            # H: the pooled hours of every instance of the item in this month


@dataclass(frozen=True)
class MonthFigures:
    month: Month
    units: int
    covered: Decimal           # hours the units cover: min(H, n x M)
    rest: Decimal              # hours beyond the units, Open Elastic: max(0, H - n x M)
    reserved: Decimal          # n x R (x the share of a part month), to the cent
    elastic: Decimal           # rest x PAYG, to the cent
    total: Decimal
    elastic_only: Decimal      # H x PAYG, to the cent: the month without Reserved
    saving: Decimal            # elastic_only - total


@dataclass(frozen=True)
class Candidate:
    units: int
    months: tuple              # MonthFigures, one per month of the term
    total: Decimal             # over the term
    elastic_only: Decimal      # over the term
    saving: Decimal            # over the term


@dataclass(frozen=True)
class Pool:
    term: str
    payg: Decimal
    rate: Decimal              # the monthly rate of one unit (for RU the upfront total divided by the months)
    model: str                 # how the months were counted, in words
    months: tuple              # Month
    candidates: tuple          # Candidate for n = 0 .. the most units any month can use
    best: Candidate            # the smallest term total, the smaller n on a tie
    threshold: Decimal         # R / PAYG in hours, exact

    @property
    def upfront(self) -> bool:
        return self.term in UPFRONT

    @property
    def term_months(self) -> int:
        return TERM_MONTHS[self.term]

    @property
    def average(self) -> bool:
        """True in the 730 h model: every month of the term is the same."""
        return self.model.startswith("730")

    def upfront_total(self, units: int | None = None) -> Decimal | None:
        """n x RU x the months of the term, invoiced at the start; None for a term that is not Upfront."""
        if not self.upfront:
            return None
        n = self.best.units if units is None else units
        return calc.evaluate("round(n * r * m, 2)", {"n": Decimal(n), "r": self.rate,
                                                     "m": Decimal(self.term_months)})

    def best_alone(self, index: int) -> int:
        """The n that would be cheapest in month `index` alone (the smaller on a tie). The term choice must weigh
        every month, so this is shown, never used."""
        best = min(self.candidates, key=lambda c: (c.months[index].total, c.units))
        return best.units

    def candidate(self, units: int) -> Candidate:
        for c in self.candidates:
            if c.units == units:
                return c
        raise PoolError("no candidate with %d units" % units)


def _ev(expression: str, **names) -> Decimal:
    return calc.evaluate(expression, {k: (v if isinstance(v, Decimal) else Decimal(v)) for k, v in names.items()})


def _add_months(day: datetime.date, months: int) -> datetime.date:
    total = day.month - 1 + months
    year, month = day.year + total // 12, total % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def calendar_months(term: str, start: datetime.date) -> list[tuple[str, int, int]]:
    """(YYYY-MM, days of the month inside the term, days of the month) for every calendar month the term touches.
    The term runs from 0:00 on the start day for its months; a start after the 1st gives a part first and last month."""
    if term not in TERM_MONTHS:
        raise PoolError("a term is one of %s" % ", ".join(TERMS))
    end = _add_months(start, TERM_MONTHS[term])          # the first day after the term
    out = []
    day = start
    while day < end:
        days_of_month = calendar.monthrange(day.year, day.month)[1]
        next_first = _add_months(day.replace(day=1), 1)
        stop = min(next_first, end)
        out.append(("%04d-%02d" % (day.year, day.month), (stop - day).days, days_of_month))
        day = stop
    return out


def months_of(term: str, start: datetime.date | None, pooled) -> tuple[list[Month], str]:
    """The months of the term with their pooled hours and the month model in words. `pooled` is one value for every
    month (a part month gets its share of it) or one value per month of the term."""
    if term not in TERM_MONTHS:
        raise PoolError("a term is one of %s" % ", ".join(TERMS))
    values = [pooled] if isinstance(pooled, (Decimal, int, str)) else list(pooled)
    try:
        values = [Decimal(v) for v in values]
    except (ArithmeticError, ValueError, TypeError):
        raise PoolError("pooled hours are numbers") from None
    if not values or any(not v.is_finite() or v < 0 for v in values):
        raise PoolError("pooled hours are numbers of zero or more")
    if start is None:
        count = TERM_MONTHS[term]
        rows = [("month %d" % (i + 1), AVERAGE_HOURS, Decimal(1)) for i in range(count)]
        model = "730 h for each month (8,760 h a year over 12 months, the average)"
    else:
        parts = calendar_months(term, start)
        rows = [(label, _ev("d * 24", d=days), _ev("d / m", d=days, m=of)) for label, days, of in parts]
        model = "the calendar months from %s (days x 24 h, first and last month pro rata)" % start.isoformat()
    if len(values) == 1:
        hours = [_ev("h * s", h=values[0], s=share) for _, _, share in rows]
    elif len(values) == len(rows):
        hours = values
    else:
        raise PoolError("give one value of pooled hours or one per month of the term (%d)" % len(rows))
    return [Month(label, m, share, h) for (label, m, share), h in zip(rows, hours)], model


def month_figures(month: Month, units: int, payg: Decimal, rate: Decimal) -> MonthFigures:
    cover = _ev("n * m", n=units, m=month.hours)
    covered = min(month.pooled, cover)
    rest = _ev("max(0, h - c)", h=month.pooled, c=cover)
    reserved = _ev("round(n * r * s, 2)", n=units, r=rate, s=month.share)
    elastic = _ev("round(x * p, 2)", x=rest, p=payg)
    total = _ev("a + b", a=reserved, b=elastic)
    elastic_only = _ev("round(h * p, 2)", h=month.pooled, p=payg)
    return MonthFigures(month, units, covered, rest, reserved, elastic, total, elastic_only,
                        _ev("e - t", e=elastic_only, t=total))


def pool(term: str, payg, rate, pooled, start: datetime.date | None = None) -> Pool:
    """Every candidate n of one billing item over the term and the best of them. Raises NotOffered for a rate of 0
    and PoolError for anything else that does not fit."""
    term = (term or "").strip().upper()
    if term not in TERM_MONTHS:
        raise PoolError("a term is one of %s" % ", ".join(TERMS))
    if payg is None or rate is None:
        raise PoolError("the rate of PAYG or %s is not readable" % term)
    payg, rate = Decimal(payg), Decimal(rate)
    if rate == 0:
        raise NotOffered("%s is not offered: the price API carries the rate 0 for it (KB-DXYK, KB-COBA)" % term)
    if payg <= 0 or rate < 0:
        raise PoolError("the PAYG rate must be larger than zero")
    months, model = months_of(term, start, pooled)
    top = 0
    for m in months:
        if m.hours > 0:
            top = max(top, int(_ev("ceil(h / m)", h=m.pooled, m=m.hours)))
    if top > MAX_UNITS:
        raise PoolError("more than %d units; check the pooled hours" % MAX_UNITS)
    candidates = []
    for n in range(top + 1):
        figures = tuple(month_figures(m, n, payg, rate) for m in months)
        total = sum((f.total for f in figures), Decimal(0))
        only = sum((f.elastic_only for f in figures), Decimal(0))
        candidates.append(Candidate(n, figures, total, only, only - total))
    best = min(candidates, key=lambda c: (c.total, c.units))
    return Pool(term, payg, rate, model, tuple(months), tuple(candidates), best, _ev("r / p", r=rate, p=payg))
