"""Server-rendered SVG charts: shapes, scaling, escaping, and CSP safety."""

from __future__ import annotations

import re

from headliner import charts
from headliner.markup import Markup


def no_inline_style(markup: str) -> bool:
    return "style=" not in markup and "<script" not in markup


def test_empty_inputs_draw_nothing() -> None:
    assert charts.bars([], label="x", ticks=[]) == ""
    assert charts.hbars([], label="x") == ""
    assert charts.swimlanes([], label="x", ticks=[]) == ""
    assert charts.data_table("x", ("a",), []) == ""


def test_bars_scale_to_the_peak_and_mark_now_and_later() -> None:
    out = charts.bars(
        [0, 5, 10, 0],
        label="Per hour",
        ticks=["00", "", "", "03"],
        highlight=1,
        average=[2.0, 2.0, 2.0, 2.0],
        muted_from=2,
    )
    heights = [float(h) for h in re.findall(r'height="([\d.]+)"', out)]
    assert heights == [0.0, 20.0, 40.0, 0.0]
    assert out.count('class="c-bar now"') == 1
    assert out.count(" later") == 2
    assert "<polyline" in out
    assert no_inline_style(out)


def test_all_zero_bars_do_not_divide_by_zero() -> None:
    out = charts.bars([0, 0, 0], label="Quiet", ticks=["", "", ""])
    assert out.count("<rect") == 3


def test_text_is_escaped_but_markup_labels_pass_through() -> None:
    out = charts.hbars(
        [
            charts.Bar("<b>x</b>", 3, href="/a?b=1&c=2"),
            charts.Bar(Markup("<em>ok</em>"), 1),
        ],
        label="A & B",
    )
    assert "&lt;b&gt;" in out
    assert "<em>ok</em>" in out
    assert 'href="/a?b=1&amp;c=2"' in out
    assert 'aria-label="A &amp; B"' in out
    assert no_inline_style(out)


def test_swimlane_dots_are_clamped_and_linked() -> None:
    out = charts.swimlanes(
        [
            ("Wire", [charts.Dot(-0.5, "early", "/article?u=1", "first")]),
            ("Daily", [charts.Dot(2.0, "late <b>")]),
        ],
        label="Timeline",
        ticks=["09:00", "12:00", "15:00"],
    )
    xs = re.findall(r'class="c-dot[^"]*" x1="([\d.]+)"', out)
    assert xs == ["0.0", "1000.0"]
    assert '<a href="/article?u=1">' in out
    assert "late &lt;b&gt;" in out
    assert out.count('class="c-grid"') == 2 * 3
    assert no_inline_style(out)
