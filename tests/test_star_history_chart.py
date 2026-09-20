"""A sketch treatment must preserve the chart's underlying daily counts."""

from datetime import date
import re
from xml.etree import ElementTree as ET

import pytest

from scripts.update_star_history import BOTTOM, HEIGHT, LEFT, RIGHT, TOP, WIDTH, nice_step, render_svg


@pytest.mark.parametrize("dates", [
    [],
    [date(2026, 1, 1)],
    [date(2026, 1, 1), date(2026, 1, 2)],
    [date(2026, 1, 1)] * 3,
    [date(2026, 1, 9), date(2026, 1, 1), date(2026, 1, 9), date(2026, 1, 4)],
])
def test_curve_preserves_daily_totals_without_overshoot(dates: list[date]) -> None:
    svg = render_svg("example/repo", dates)
    root = ET.fromstring(svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    path = root.find("s:path[@class='line']", ns).attrib["d"]
    endpoint = root.find("s:circle[@class='endpoint']", ns)
    step = nice_step(len(dates))
    y_max = max(step, ((max(len(dates), 1) + step - 1) // step) * step)

    def y(count: int) -> float:
        return HEIGHT - BOTTOM - count / y_max * (HEIGHT - TOP - BOTTOM)

    assert float(endpoint.attrib["cy"]) == pytest.approx(y(len(dates)), abs=.06)
    assert svg == render_svg("example/repo", list(reversed(dates)))
    if not dates:
        assert "No stars yet" in svg
        return

    days = sorted(set(dates))
    span = max((days[-1] - days[0]).days, 1)
    previous_x, previous_y = LEFT, round(y(dates.count(days[0])), 1)
    segments = re.findall(r"C ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)", path)
    assert len(segments) == len(days) - 1
    for day, segment in zip(days[1:], segments):
        x1, y1, x2, y2, end_x, end_y = map(float, segment)
        # Ordered Bezier controls guarantee monotonicity between daily totals.
        assert previous_x <= x1 <= x2 <= end_x
        assert previous_y >= y1 >= y2 >= end_y
        expected_x = LEFT + (day - days[0]).days / span * (WIDTH - LEFT - RIGHT)
        assert end_x == pytest.approx(expected_x, abs=.06)
        assert end_y == pytest.approx(y(sum(d <= day for d in dates)), abs=.06)
        previous_x, previous_y = end_x, end_y
    assert float(endpoint.attrib["cx"]) == previous_x


def test_chart_escapes_repository_labels() -> None:
    svg = render_svg("a/<unsafe>&repo", [date(2026, 1, 1)])
    root = ET.fromstring(svg)
    assert "a/<unsafe>&repo" in "".join(root.itertext())
    assert "<unsafe>" not in svg
