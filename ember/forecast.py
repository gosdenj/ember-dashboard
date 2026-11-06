"""The forecast: how much gas tomorrow, from how much you used today.

The model is a straight line, but not one that goes through the origin:

    gas_m3 = baseline + k x degree_days

``degree_days`` is ``max(0, base_temperature - mean outside temperature)``
for the day - a measure of how cold it was. ``k`` is the slope, gas per
degree-day, and ``baseline`` is the intercept.

**The intercept is not optional with a combi boiler.** Hot water and
cooking happen on roughly the same days whatever the weather, so they
belong in the constant, not in the heating term. A line forced through
the origin would push that constant use into the slope and over-predict
every mild day - which, in this country, is most days.

The fit is deliberately robust. A single odd day - guests over, a freezer
defrosting, a boiler fault - should not throw the whole model out, so the
fit is done twice: once for everything, then again with the points that
sit implausibly far from the first line removed.

The result always comes with the number of days that went into it and the
width of the band around it. A forecast that does not say how sure it is
is not much of a forecast.
"""

from __future__ import annotations

import math
from typing import Sequence


def degree_days(mean_outside: float | None, base_temperature: float) -> float:
    """Heating degree-days for one day."""
    if mean_outside is None:
        return 0.0
    return max(0.0, base_temperature - mean_outside)


class ForecastModel:
    """A fitted line, with the numbers needed to judge it."""

    def __init__(self, baseline: float, slope: float, days_used: int,
                 sigma: float, residuals: list[float], r_squared: float,
                 base_temperature: float, dropped: int = 0):
        self.baseline = baseline
        self.slope = slope
        self.days_used = days_used
        self.sigma = sigma
        self.residuals = residuals
        self.r_squared = r_squared
        self.base_temperature = base_temperature
        self.dropped = dropped

    def predict(self, dd: float) -> float:
        return max(0.0, self.baseline + self.slope * dd)

    def band(self, dd: float, multiplier: float = 1.96) -> float:
        """Half-width of the confidence band around a prediction."""
        return max(0.05, multiplier * self.sigma)

    def to_dict(self) -> dict:
        return {
            "baseline_m3": round(self.baseline, 3),
            "slope_m3_per_degree_day": round(self.slope, 4),
            "days_used": self.days_used,
            "days_dropped": self.dropped,
            "sigma_m3": round(self.sigma, 3),
            "r_squared": round(self.r_squared, 3),
            "base_temperature": self.base_temperature,
        }


class ForecastUnavailable(Exception):
    """There is not enough history to forecast yet."""


def _least_squares(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Ordinary least squares. Returns (intercept, slope)."""
    n = len(points)
    if n < 2:
        raise ForecastUnavailable("need at least two points")
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x, _ in points)
    sum_xy = sum(x * y for x, y in points)
    denominator = n * sum_xx - sum_x * sum_x
    if abs(denominator) < 1e-9:
        raise ForecastUnavailable("the degree-days are all the same - "
                                  "there is no slope to fit")
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return intercept, slope


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _mad(values: Sequence[float], centre: float) -> float:
    return _median([abs(value - centre) for value in values])


def fit(pairs: list[tuple[float, float]], base_temperature: float,
        minimum_days: int = 7) -> ForecastModel:
    """Fit the model to (degree_days, gas_m3) pairs.

    Raises ForecastUnavailable when there is not enough to go on, rather
    than returning a confident-looking number built from three days.
    """
    usable = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(usable) < minimum_days:
        raise ForecastUnavailable(
            f"only {len(usable)} days of history - {minimum_days} needed")

    intercept, slope = _least_squares(usable)
    residuals = [y - (intercept + slope * x) for x, y in usable]
    centre = _median(residuals)
    spread = _mad(residuals, centre)

    dropped = 0
    if len(usable) >= minimum_days + 3 and spread > 1e-6:
        # Anything more than three median-absolute-deviations from the
        # line is not a cold day, it is a different day.
        threshold = 3.0 * spread
        kept = [(x, y) for (x, y), r in zip(usable, residuals) if abs(r - centre) <= threshold]
        dropped = len(usable) - len(kept)
        if len(kept) >= minimum_days:
            intercept, slope = _least_squares(kept)
            usable = kept
            residuals = [y - (intercept + slope * x) for x, y in usable]

    # A combi's hot water use cannot be negative, and neither can a
    # baseline: if the fit says the intercept is below zero, the data is
    # too thin or too warm to trust it.
    baseline = max(0.0, intercept)

    if len(usable) > 2:
        sigma = math.sqrt(sum(r * r for r in residuals) / (len(usable) - 2))
    else:
        sigma = 0.0

    mean_y = sum(y for _, y in usable) / len(usable)
    total = sum((y - mean_y) ** 2 for _, y in usable)
    residual_total = sum(r * r for r in residuals)
    r_squared = 1.0 - (residual_total / total) if total > 1e-9 else 0.0

    return ForecastModel(baseline, slope, len(usable), sigma, residuals,
                          max(0.0, min(1.0, r_squared)), base_temperature, dropped)
