"""Charts the server renders as SVG, from the rows.

The operator's decision on 2026-09-05, `prd.md:829-836`: no chart library,
nothing drawn on the client, and a tap on a bar is a link the SVG carries and
never a handler. A chart that is markup is read by a test without a browser,
prints, and needs no second script under the policy.

Four of the five are here -- the Backlog's age histogram, Today's day
timeline, and the Usage screen's burn line and gauges (sd:234 slice 8d);
the week timeline is still to come.

Every dimension is in the SVG's own coordinate space with `viewBox` and no
fixed width, so the chart scales to the column it sits in and no screen needs
a horizontal scroll to read it (`prd.md:722`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .markup import Markup, join, tag

__all__ = ["age_histogram_svg", "burn_svg", "gauge_svg", "timeline_svg"]

BAR_HEIGHT = 26
BAR_GAP = 8
LABEL_WIDTH = 74
CHART_WIDTH = 600


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_histogram_svg(buckets, *, link, selected: str | None = None) -> Markup:
    """Open items by days in their current status, `ready_to_send` its own series.

    `link(bucket, series)` returns the filter the bar stands for --
    requirement 5's "a bar is a filter on the list" -- and it is an `a`
    element wrapping the rect, so the tap is a navigation the browser performs
    and not a script.

    `selected` is the bucket key the list is currently narrowed to. It is
    *marked*, with a class and `aria-current`, and never hidden: the other
    bars stay tappable, because this is a picker and not a one-way door.
    """
    rows = [bucket for bucket in buckets]
    biggest = max([bucket.total for bucket in rows] or [0]) or 1
    height = len(rows) * (BAR_HEIGHT + BAR_GAP) + BAR_GAP
    span = CHART_WIDTH - LABEL_WIDTH - 60
    bars: list[object] = []
    for index, bucket in enumerate(rows):
        y = BAR_GAP + index * (BAR_HEIGHT + BAR_GAP)
        x = LABEL_WIDTH
        bars.append(
            tag(
                "text",
                bucket.label,
                x=LABEL_WIDTH - 8,
                y=y + BAR_HEIGHT - 8,
                class_="chart-label",
                text_anchor="end",
            )
        )
        here = selected is not None and bucket.key == selected
        for series in ("ready_to_send", "other"):
            count = bucket.counts.get(series, 0)
            if not count:
                continue
            width = max(2, round(span * count / biggest))
            classes = f"chart-bar chart-bar-{series.replace('_', '-')}"
            if here:
                classes += " chart-bar-selected"
            bars.append(
                tag(
                    "a",
                    tag(
                        "rect",
                        x=x,
                        y=y,
                        width=width,
                        height=BAR_HEIGHT,
                        class_=classes,
                    ),
                    tag(
                        "title",
                        f"{count} {series.replace('_', ' ')} at {bucket.label}",
                    ),
                    href=link(bucket, series),
                    aria_current="true" if here else None,
                    data_age=bucket.key,
                    data_series=series,
                )
            )
            x += width
        bars.append(
            tag(
                "text",
                str(bucket.total),
                x=x + 6,
                y=y + BAR_HEIGHT - 8,
                class_="chart-value",
            )
        )
    return tag(
        "svg",
        tag("title", "Open items by days in their current status"),
        join(bars),
        viewBox=f"0 0 {CHART_WIDTH} {height}",
        role="img",
        class_="chart chart-histogram",
        preserveAspectRatio="xMinYMin meet",
    )


def timeline_svg(rows: list[sqlite3.Row], *, now: str, href) -> Markup:
    """The day's assignments: one lane per repository, one bar per row.

    A wait is drawn hatched with what it waited on, and `budget_minutes` is a
    mark on the bar, so what ran, what waited and what overran is read from
    position. A bar is a link to the row.
    """
    end = _parse(now) or datetime.now(timezone.utc)
    start_of_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = max(60, int((end - start_of_day).total_seconds() // 60) or 60)
    lanes: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        lanes.setdefault(row["lane"], []).append(row)
    height = len(lanes) * (BAR_HEIGHT + BAR_GAP) + BAR_GAP + 18
    span = CHART_WIDTH - LABEL_WIDTH - 10

    def position(stamp: str | None, fallback: datetime) -> float:
        moment = _parse(stamp) or fallback
        offset = (moment - start_of_day).total_seconds() / 60
        return LABEL_WIDTH + max(0.0, min(1.0, offset / minutes)) * span

    parts: list[object] = [
        tag(
            "defs",
            Markup(
                '<pattern id="wait" width="6" height="6" patternUnits="userSpaceOnUse" '
                'patternTransform="rotate(45)">'
                '<line x1="0" y1="0" x2="0" y2="6" class="chart-hatch" />'
                "</pattern>"
            ),
        )
    ]
    for index, (lane, lane_rows) in enumerate(sorted(lanes.items())):
        y = BAR_GAP + index * (BAR_HEIGHT + BAR_GAP)
        parts.append(
            tag(
                "text",
                lane.rsplit("/", 1)[-1],
                x=LABEL_WIDTH - 8,
                y=y + BAR_HEIGHT - 8,
                class_="chart-label",
                text_anchor="end",
            )
        )
        for row in lane_rows:
            left = position(row["started"], start_of_day)
            right = position(row["ended"], end)
            width = max(3.0, right - left)
            waited = row["status"] == "queued"
            bar = tag(
                "rect",
                x=round(left, 2),
                y=y + 4,
                width=round(width, 2),
                height=BAR_HEIGHT - 8,
                class_=f"chart-run chart-run-{row['status']}",
                fill="url(#wait)" if waited else None,
            )
            label = f"assignment {row['id']} — {row['role']} — {row['status']}"
            marks: list[object] = [bar, tag("title", label)]
            budget = row["budget_minutes"]
            if budget:
                mark = position(row["started"], start_of_day) + span * min(
                    1.0, float(budget) / minutes
                )
                marks.append(
                    tag(
                        "line",
                        x1=round(min(mark, LABEL_WIDTH + span), 2),
                        x2=round(min(mark, LABEL_WIDTH + span), 2),
                        y1=y,
                        y2=y + BAR_HEIGHT,
                        class_="chart-budget",
                    )
                )
            parts.append(tag("a", join(marks), href=href(row)))
    parts.append(
        tag(
            "text",
            f"00:00 to {end.strftime('%H:%M')}",
            x=LABEL_WIDTH,
            y=height - 4,
            class_="chart-axis",
        )
    )
    return tag(
        "svg",
        tag("title", "The day's assignments by repository"),
        join(parts),
        viewBox=f"0 0 {CHART_WIDTH} {height}",
        role="img",
        class_="chart chart-timeline",
        preserveAspectRatio="xMinYMin meet",
    )


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
