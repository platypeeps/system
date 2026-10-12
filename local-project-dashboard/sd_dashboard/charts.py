"""Charts the server renders as SVG, from the rows.

The operator's decision on 2026-09-05, `prd.md:829-836`: no chart library,
nothing drawn on the client, and a tap on a bar is a link the SVG carries and
never a handler. A chart that is markup is read by a test without a browser,
prints, and needs no second script under the policy.

Two are left: the Usage screen's burn line and gauges (sd:234 slice 8d).
The age histogram and the day timeline went with the classic Progress area
and the old Today (sd:3402); Metrics and Activity draw those now.

Every dimension is in the SVG's own coordinate space with `viewBox` and no
fixed width, so the chart scales to the column it sits in and no screen needs
a horizontal scroll to read it (`prd.md:722`).
"""

from __future__ import annotations


from .markup import Markup, join, tag

__all__ = ["burn_svg", "gauge_svg"]

BAR_HEIGHT = 26
LABEL_WIDTH = 74
CHART_WIDTH = 600


def burn_svg(bill, *, days: int, today: int | None) -> Markup:
    """A capped bill's burn: spend to date as a line across the month, the cap
    as a rule, and a straight projection from the month's rate to month end.

    `bill` is a `reads.BillUsage`; `days` is the month's length and `today`
    the day of the month `now` falls on, None in another month. The points
    are `(day, cumulative)`, the rate is the last point over the days
    elapsed, and the projection is drawn only for the month `now` is in. `data-cross` names the day the projection
    passes the cap, so a cap the month will pass is read before it is
    passed.
    """
    points, cap = list(bill.burn), bill.cap
    height, left, top, bottom = 200, LABEL_WIDTH, 12, 24
    span, drop = CHART_WIDTH - left - 10, height - top - bottom
    top_value = max([cap or 0.0] + [value for _, value in points]) or 1.0

    def x(day: float) -> float:
        return round(left + span * (day - 1) / max(1, days - 1), 2)

    def y(value: float) -> float:
        return round(top + drop - drop * value / top_value, 2)

    parts: list[object] = []
    if cap is not None:
        parts.append(tag("line", x1=left, x2=left + span, y1=y(cap), y2=y(cap), class_="chart-cap",
                         data_cap=f"{cap:.2f}"))
    line = ([(1, 0.0)] if not points or points[0][0] != 1 else []) + points
    if points and today and today > points[-1][0]:
        # Flat since the last row: the line reaches today before the
        # projection leaves it.
        line.append((today, points[-1][1]))
    parts.append(tag("polyline", points=" ".join(f"{x(day)},{y(value)}" for day, value in line),
                     class_="chart-burn", data_points=";".join(f"{day}:{value:.2f}" for day, value in points)))
    for day, value in points:
        parts.append(tag("circle", cx=x(day), cy=y(value), r=3, class_="chart-point", data_day=day,
                         data_value=f"{value:.2f}"))
    if points and today:
        spent, elapsed = points[-1][1], max(1, today)
        rate = spent / elapsed
        end = spent + rate * (days - elapsed)
        cross = None
        if cap is not None and rate > 0 and end > cap:
            # A cap already passed (a zero cap, say) crossed today at the latest.
            cross = elapsed + max(0.0, cap - spent) / rate
        # A projection past the top of the chart stops where it leaves it,
        # at the day the rate puts it there, so the drawn slope is the rate.
        # Clamping the value alone kept `x2` at the month's end and pulled
        # the line down to meet the top rule there: forty by day eight is a
        # hundred on day 20, `data-cross` said 20, and the line met the cap
        # rule on day 30 (PR #430 review).
        until = days if end <= top_value else elapsed + (top_value - spent) / rate
        parts.append(tag("line", x1=x(elapsed), y1=y(spent), x2=x(until), y2=y(min(end, top_value)),
                         class_="chart-projection", data_end=f"{end:.2f}",
                         data_cross=None if cross is None else str(int(cross) + (0 if cross == int(cross) else 1))))
    parts.append(tag("text", f"1 to {days}", x=left, y=height - 6, class_="chart-axis"))
    parts.append(tag("text", f"${top_value:,.0f}", x=left - 8, y=top + 4, class_="chart-label", text_anchor="end"))
    return tag("svg", tag("title", f"{bill.name}: spend to date, cap and projection"), join(parts),
               viewBox=f"0 0 {CHART_WIDTH} {height}", role="img", class_="chart chart-burn-line",
               preserveAspectRatio="xMinYMin meet")


def gauge_svg(label: str, percent: float | None, *, name: str) -> Markup:
    """One gauge: a bar filled to `percent`, the value beside it.

    A `plan` bill draws two of these from its `meter` rows, the five-hour and
    the weekly window; a capped bill draws one for spend against its cap.
    `data-percent` carries the number a test reads.
    """
    value = None if percent is None else max(0.0, min(100.0, float(percent)))
    width = 0 if value is None else round((CHART_WIDTH - LABEL_WIDTH - 60) * value / 100)
    return tag("svg", tag("title", f"{name}: {label}"),
               tag("text", label, x=LABEL_WIDTH - 8, y=BAR_HEIGHT - 8, class_="chart-label", text_anchor="end"),
               tag("rect", x=LABEL_WIDTH, y=0, width=CHART_WIDTH - LABEL_WIDTH - 60, height=BAR_HEIGHT, class_="gauge-track"),
               tag("rect", x=LABEL_WIDTH, y=0, width=width, height=BAR_HEIGHT, class_="gauge-fill",
                   data_gauge=name, data_percent="" if value is None else f"{value:.0f}"),
               tag("text", "no reading" if value is None else f"{value:.0f}%", x=CHART_WIDTH - 54,
                   y=BAR_HEIGHT - 8, class_="chart-value"),
               viewBox=f"0 0 {CHART_WIDTH} {BAR_HEIGHT}", role="img", class_="chart chart-gauge",
               preserveAspectRatio="xMinYMin meet")
