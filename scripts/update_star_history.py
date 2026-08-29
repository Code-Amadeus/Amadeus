#!/usr/bin/env python3
"""Generate a privacy-preserving star-history SVG from the GitHub API."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_VERSION = "2026-03-10"
WIDTH = 900
HEIGHT = 280
LEFT = 72
RIGHT = 28
TOP = 54
BOTTOM = 48


def fetch_star_dates(repository: str, token: str) -> list[date]:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name:
        raise ValueError("repository must use the OWNER/REPO form")

    dates: list[date] = []
    page = 1
    while True:
        url = (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(name, safe='')}/stargazers"
            f"?per_page=100&page={page}"
        )
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github.star+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "amadeus-star-history",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                items = json.load(response)
        except HTTPError as error:
            raise RuntimeError(
                f"GitHub stargazers request failed with HTTP {error.code}"
            ) from error

        if not isinstance(items, list):
            raise RuntimeError("GitHub returned an unexpected stargazers response")
        for item in items:
            timestamp = item.get("starred_at")
            if not timestamp:
                raise RuntimeError(
                    "GitHub did not return star timestamps; check token repository access"
                )
            dates.append(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date())

        if len(items) < 100:
            break
        page += 1

    return sorted(dates)


def nice_step(maximum: int, tick_count: int = 4) -> int:
    if maximum <= 1:
        return 1
    rough = maximum / tick_count
    magnitude = 10 ** math.floor(math.log10(rough))
    normalized = rough / magnitude
    factor = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    return factor * magnitude


def render_svg(repository: str, star_dates: list[date]) -> str:
    counts = Counter(star_dates)
    if star_dates:
        start = star_dates[0]
        end = star_dates[-1]
    else:
        start = end = date.today()

    span_days = max((end - start).days, 1)
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM
    step = nice_step(len(star_dates))
    y_max = max(step, math.ceil(max(len(star_dates), 1) / step) * step)

    def x_position(day: date) -> float:
        return LEFT + ((day - start).days / span_days) * plot_width

    def y_position(value: int) -> float:
        return TOP + plot_height - (value / y_max) * plot_height

    cumulative = 0
    events: list[tuple[date, int]] = []
    for day in sorted(counts):
        cumulative += counts[day]
        events.append((day, cumulative))

    if events:
        first_x = x_position(events[0][0])
        commands = [f"M {first_x:.1f} {y_position(0):.1f}"]
        for day, value in events:
            x = x_position(day)
            y = y_position(value)
            commands.append(f"L {x:.1f} {y:.1f}")
        line_path = " ".join(commands)
        final_x = x_position(events[-1][0])
        final_y = y_position(events[-1][1])
    else:
        line_path = f"M {LEFT:.1f} {y_position(0):.1f} H {LEFT + plot_width:.1f}"
        final_x = LEFT + plot_width
        final_y = y_position(0)

    x_tick_offsets = sorted({round(span_days * index / 4) for index in range(5)})
    y_ticks = list(range(0, y_max + 1, step))
    if y_ticks[-1] != y_max:
        y_ticks.append(y_max)

    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-labelledby="title desc" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<title id=\"title\">Amadeus star history</title>",
        "<desc id=\"desc\">Cumulative GitHub stars over time. No user data is stored.</desc>",
        "<style>",
        ".background{fill:#ffffff}.grid{stroke:#d8dee4;stroke-width:1}.axis{fill:#57606a;font:12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.heading{fill:#24292f;font:600 16px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.meta{fill:#6e7781;font:12px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.line{fill:none;stroke:#2f81f7;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.endpoint{fill:#2f81f7}",
        "@media(prefers-color-scheme:dark){.background{fill:#0d1117}.grid{stroke:#30363d}.axis{fill:#8b949e}.heading{fill:#e6edf3}.meta{fill:#8b949e}.line{stroke:#58a6ff}.endpoint{fill:#58a6ff}}",
        "</style>",
        f'<rect class="background" x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="12"/>',
        f'<text class="heading" x="{LEFT}" y="25">Star growth · {len(star_dates)}</text>',
        f'<text class="meta" x="{LEFT}" y="43">{escape(repository)} · latest star {end.isoformat()}</text>',
    ]

    for value in y_ticks:
        y = y_position(value)
        lines.append(
            f'<line class="grid" x1="{LEFT}" y1="{y:.1f}" x2="{LEFT + plot_width}" y2="{y:.1f}"/>'
        )
        lines.append(
            f'<text class="axis" x="{LEFT - 12}" y="{y + 4:.1f}" text-anchor="end">{value}</text>'
        )

    for offset in x_tick_offsets:
        day = start.fromordinal(start.toordinal() + offset)
        x = LEFT + (offset / span_days) * plot_width
        anchor = "start" if offset == 0 else "end" if offset == span_days else "middle"
        lines.append(
            f'<text class="axis" x="{x:.1f}" y="{HEIGHT - 18}" text-anchor="{anchor}">{day.isoformat()}</text>'
        )

    lines.append(f'<path class="line" d="{line_path}"/>')
    lines.append(f'<circle class="endpoint" cx="{final_x:.1f}" cy="{final_y:.1f}" r="4"/>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="GitHub repository as OWNER/REPO")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN or GH_TOKEN is required")

    star_dates = fetch_star_dates(args.repository, token)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(args.repository, star_dates), encoding="utf-8")
    print(f"Wrote {args.output} with {len(star_dates)} current stars")


if __name__ == "__main__":
    main()
