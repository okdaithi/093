"""Small server-rendered charts for the web viewer.

Every chart is inline SVG built from attributes only (the viewer's Content
Security Policy forbids inline styles and scripts); colours come from CSS
classes so light and dark themes both work. Text stays in HTML around the SVG,
so stretching a chart to the page width never distorts its labels. Each chart
can carry a `data_table` with the same numbers for screen readers and copying.

Functions take plain numbers and preformatted labels and return `Markup`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from headliner.markup import EMPTY, Markup, join, render

BAR_HEIGHT: Final = 40.0
BAR_WIDTH: Final = 10.0
BAR_GAP: Final = 2.0


def _figure(kind: str, label: str, chart: Markup, extra: Markup = EMPTY) -> Markup:
    return render(
        '<figure class="chart {k}" role="group" aria-label="{l}">{c}{e}</figure>',
        k=kind,
        l=label,
        c=chart,
        e=extra,
    )


def data_table(caption: str, headers: Sequence[str], rows: Sequence[Sequence[object]]) -> Markup:
    """The chart's numbers as a table, folded away under "Data"."""
    if not rows:
        return EMPTY
    return render(
        '<details class="chart-data"><summary>Data</summary><div class="scroll"><table>'
        "<caption>{c}</caption><thead><tr>{h}</tr></thead><tbody>{r}</tbody></table></div>"
        "</details>",
        c=caption,
        h=join(render("<th>{h}</th>", h=header) for header in headers),
        r=join(
            render("<tr>{cells}</tr>", cells=join(render("<td>{v}</td>", v=v) for v in row))
            for row in rows
        ),
    )


def bars(
    values: Sequence[int],
    *,
    label: str,
    ticks: Sequence[str],
    titles: Sequence[str] | None = None,
    highlight: int | None = None,
    average: Sequence[float] | None = None,
    muted_from: int | None = None,
) -> Markup:
    """Vertical bars, one per value, with an optional average line over them.

    `ticks` are the labels under the bars, one per bar ("" for none).
    `highlight` marks one bar (e.g. the current hour); bars from `muted_from`
    on are drawn faded (e.g. hours still to come).
    """
    if not values:
        return EMPTY
    peak = max([*values, *(average or [])]) or 1
    width = len(values) * BAR_WIDTH
    rects = []
    for index, value in enumerate(values):
        height = BAR_HEIGHT * value / peak
        css = "c-bar"
        if index == highlight:
            css += " now"
        if muted_from is not None and index >= muted_from:
            css += " later"
        rects.append(
            render(
                '<rect class="{c}" x="{x}" y="{y}" width="{w}" height="{h}">'
                "<title>{t}</title></rect>",
                c=css,
                x=f"{index * BAR_WIDTH + BAR_GAP / 2:g}",
                y=f"{BAR_HEIGHT - height:.2f}",
                w=f"{BAR_WIDTH - BAR_GAP:g}",
                h=f"{height:.2f}",
                t=titles[index] if titles else str(value),
            )
        )
    line = EMPTY
    if average:
        points = " ".join(
            f"{(index + 0.5) * BAR_WIDTH:g},{BAR_HEIGHT - BAR_HEIGHT * value / peak:.2f}"
            for index, value in enumerate(average)
        )
        line = render(
            '<polyline class="c-avg" points="{p}" vector-effect="non-scaling-stroke"/>', p=points
        )
    svg = render(
        '<svg class="bars" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        'aria-label="{l}">{r}{a}</svg>',
        w=f"{width:g}",
        h=f"{BAR_HEIGHT:g}",
        l=label,
        r=join(rects),
        a=line,
    )
    axis = render(
        '<div class="axis even">{t}</div>', t=join(render("<span>{t}</span>", t=t) for t in ticks)
    )
    return _figure("bar-chart", label, svg, axis)


@dataclass(frozen=True, slots=True)
class Bar:
    label: Markup | str
    value: int
    note: str = ""
    href: str | None = None


def hbars(rows: Sequence[Bar], *, label: str) -> Markup:
    """Horizontal bars, one row each: label, bar scaled to the largest, value."""
    if not rows:
        return EMPTY
    peak = max(row.value for row in rows) or 1

    def one(row: Bar) -> Markup:
        name = render('<a href="{h}">{l}</a>', h=row.href, l=row.label) if row.href else row.label
        return render(
            '<li><span class="hb-label">{n}</span>'
            '<svg class="hb-bar" viewBox="0 0 100 10" preserveAspectRatio="none" '
            'aria-hidden="true">'
            '<rect class="c-bar" x="0" y="1" width="{w}" height="8"/></svg>'
            '<span class="hb-value" title="{note}">{v}</span></li>',
            n=name,
            w=f"{100 * row.value / peak:.2f}",
            note=row.note,
            v=f"{row.value:,}",
        )

    return _figure(
        "hbar-chart",
        label,
        render('<ol class="hbars">{r}</ol>', r=join(one(row) for row in rows)),
    )


@dataclass(frozen=True, slots=True)
class Dot:
    """A point on a timeline: `at` is 0..1 across the chart."""

    at: float
    title: str
    href: str | None = None
    css: str = ""


def swimlanes(
    lanes: Sequence[tuple[Markup | str, Sequence[Dot]]], *, label: str, ticks: Sequence[str]
) -> Markup:
    """One row per lane with dots along a shared time axis.

    `ticks` label evenly spaced points from the start to the end of the axis,
    so they line up with the faint grid lines drawn in every lane.
    """
    if not lanes:
        return EMPTY
    grid = (
        join(
            render(
                '<line class="c-grid" x1="{x}" y1="0" x2="{x}" y2="20" '
                'vector-effect="non-scaling-stroke"/>',
                x=f"{1000 * index / (len(ticks) - 1):.1f}",
            )
            for index in range(len(ticks))
        )
        if len(ticks) > 1
        else EMPTY
    )

    def dot(point: Dot) -> Markup:
        # A zero-length line with round caps is a circle that stays round
        # however much the chart is stretched.
        mark = render(
            '<line class="c-dot {c}" x1="{x}" y1="10" x2="{x}" y2="10" '
            'vector-effect="non-scaling-stroke"><title>{t}</title></line>',
            c=point.css,
            x=f"{1000 * min(max(point.at, 0.0), 1.0):.1f}",
            t=point.title,
        )
        return render('<a href="{h}">{m}</a>', h=point.href, m=mark) if point.href else mark

    rows = join(
        render(
            '<li><span class="lane-label">{l}</span>'
            '<svg class="lane" viewBox="0 0 1000 20" preserveAspectRatio="none" aria-hidden="true">'
            '{g}<line class="c-track" x1="0" y1="10" x2="1000" y2="10" '
            'vector-effect="non-scaling-stroke"/>{d}</svg></li>',
            l=name,
            g=grid,
            d=join(dot(point) for point in points),
        )
        for name, points in lanes
    )
    axis = render(
        '<li class="lane-axis"><span class="lane-label"></span>'
        '<span class="axis spread">{t}</span></li>',
        t=join(render("<span>{t}</span>", t=t) for t in ticks),
    )
    return _figure("timeline", label, render('<ol class="lanes">{r}{a}</ol>', r=rows, a=axis))
