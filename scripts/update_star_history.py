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
WIDTH = 760
HEIGHT = 620
LEFT = 80
RIGHT = 58
TOP = 138
BOTTOM = 100


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
    return max(1, int(factor * magnitude))


def render_svg(repository: str, star_dates: list[date]) -> str:
    counts = Counter(star_dates)
    if star_dates:
        start = min(star_dates)
        end = max(star_dates)
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
        first_y = y_position(events[0][1])
        commands.append(f"L {first_x:.1f} {first_y:.1f}")
        points = [(x_position(day), y_position(value)) for day, value in events]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            # Horizontal endpoint tangents keep the curve smooth and monotone.
            # Every daily total remains on the curve; no overshoot or fake noise.
            third = (x1 - x0) / 3
            commands.append(
                f"C {x0 + third:.1f} {y0:.1f} {x1 - third:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"
            )
        line_path = " ".join(commands)
        final_x = x_position(events[-1][0])
        final_y = y_position(events[-1][1])
    else:
        line_path = f"M {LEFT:.1f} {y_position(0):.1f} H {LEFT + plot_width:.1f}"
        final_x = LEFT + plot_width
        final_y = y_position(0)

    x_tick_offsets = sorted({round((end - start).days * index / 3) for index in range(4)})
    y_ticks = list(range(0, y_max + 1, step))
    if y_ticks[-1] != y_max:
        y_ticks.append(y_max)

    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-labelledby="title desc" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<title id=\"title\">Amadeus star history</title>",
        '<desc id="desc">Cumulative counts of current GitHub stargazers by star date. '
        'The curve passes through every daily total. No user identities are stored.</desc>',
        '<defs><filter id="pen" x="-2%" y="-2%" width="104%" height="104%">',
        '<feTurbulence type="fractalNoise" baseFrequency=".035 .14" numOctaves="1" seed="7" result="grain"/>',
        '<feDisplacementMap in="SourceGraphic" in2="grain" scale="1.1" xChannelSelector="R" yChannelSelector="G"/>',
        '</filter></defs>',
        "<style>",
        ".background{fill:#fcfcf8;stroke:#e2e8df}.grid{stroke:#dce4dc;stroke-width:1;stroke-dasharray:3 7}"
        ".axis{fill:#53675d;font:15px 'Segoe Print','Comic Sans MS',cursive}"
        ".heading{fill:#203c2e;font:700 30px 'Segoe Print','Comic Sans MS',cursive}"
        ".total{fill:#286749;font:700 28px 'Segoe Print','Comic Sans MS',cursive}"
        ".meta{fill:#64756c;font:14px 'Segoe UI',sans-serif}"
        ".line{fill:none;stroke:#348663;stroke-width:3.4;stroke-linecap:round;stroke-linejoin:round;filter:url(#pen)}"
        ".baseline{fill:none;stroke:#6b8072;stroke-width:1.5;stroke-linecap:round;filter:url(#pen)}"
        ".endpoint{fill:#348663;stroke:#fcfcf8;stroke-width:2}",
        "@media(prefers-color-scheme:dark){.background{fill:#0d1712;stroke:#2d4034}.grid{stroke:#304238}"
        ".axis,.meta{fill:#a1b7a9}.heading{fill:#e2eee5}.total{fill:#9bd8b6}"
        ".line{stroke:#83cba5}.endpoint{fill:#83cba5;stroke:#0d1712}.baseline{stroke:#819c8a}}",
        "</style>",
        f'<rect class="background" x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="12"/>',
        f'<text class="heading" x="{LEFT - 22}" y="59">Star History</text>',
        f'<text class="total" x="{WIDTH - RIGHT}" y="59" text-anchor="end">{len(star_dates)} stars</text>',
        f'<text class="meta" x="{LEFT - 22}" y="89">{escape(repository)}</text>',
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
            f'<text class="axis" x="{x:.1f}" y="{HEIGHT - BOTTOM + 32}" text-anchor="{anchor}">{day.isoformat()}</text>'
        )

    lines.append(
        f'<path class="baseline" d="M {LEFT} {TOP - 8} V {HEIGHT - BOTTOM} H {WIDTH - RIGHT + 8}"/>'
    )
    lines.append(f'<path class="line" d="{line_path}"/>')
    lines.append(f'<circle class="endpoint" cx="{final_x:.1f}" cy="{final_y:.1f}" r="5"/>')
    caption = f"Latest star · {end.isoformat()}" if star_dates else "No stars yet"
    lines.append(f'<text class="meta" x="{LEFT}" y="{HEIGHT - 36}">Cumulative count of current stargazers</text>')
    lines.append(f'<text class="meta" x="{WIDTH - RIGHT}" y="{HEIGHT - 36}" text-anchor="end">{caption}</text>')
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
