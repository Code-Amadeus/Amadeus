import argparse
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


LOG_TS_BRACKET_RE = re.compile(r"^\[(?P<ts>\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2})\]")
# 与 main.py logging.basicConfig 默认 asctime 一致：YYYY-MM-DD HH:MM:SS,mmm
LOG_TS_ISO_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(?P<ms>\d+))?"
)
MODE_RE = re.compile(r"\[TTS Mode\].*CUDA_Graph=(?P<graph>ON|OFF), semaphore=(?P<sem>\d+)")
SENTENCE_ID_RE = re.compile(r"sentence_(?P<seq>\d+)_")


def parse_log_time(line: str) -> datetime | None:
    match = LOG_TS_BRACKET_RE.search(line)
    if match:
        return datetime.strptime(match.group("ts"), "%d/%b/%Y %H:%M:%S")
    match = LOG_TS_ISO_RE.search(line)
    if match:
        base = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
        ms = match.group("ms")
        if ms is not None:
            base = base + timedelta(milliseconds=int(ms))
        return base
    return None


def sentence_seq(line: str) -> int | None:
    match = SENTENCE_ID_RE.search(line)
    if not match:
        return None
    return int(match.group("seq"))


@dataclass
class TurnMetrics:
    source: str
    mode: str = "unknown"
    round_index: int = 0
    request_sent_at: datetime | None = None
    api_start_at: datetime | None = None
    first_sentence_seen_at: datetime | None = None
    first_tts_lock_at: datetime | None = None
    first_chunk_generated_at: datetime | None = None
    first_play_at: datetime | None = None
    first_physical_play_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def start_time(self) -> datetime | None:
        return self.request_sent_at or self.api_start_at

    def first_audible_time(self) -> datetime | None:
        return self.first_play_at or self.first_physical_play_at

    def metric_ms(self, start: datetime | None, end: datetime | None) -> float | None:
        if start is None or end is None:
            return None
        return (end - start).total_seconds() * 1000.0

    def as_row(self) -> dict:
        start = self.start_time()
        first_audible = self.first_audible_time()
        return {
            "source": self.source,
            "mode": self.mode,
            "round": self.round_index,
            "send_to_first_play_ms": self.metric_ms(start, first_audible),
            "send_to_first_sentence_ms": self.metric_ms(start, self.first_sentence_seen_at),
            "send_to_first_tts_lock_ms": self.metric_ms(start, self.first_tts_lock_at),
            "send_to_first_chunk_ms": self.metric_ms(start, self.first_chunk_generated_at),
            "first_sentence_to_play_ms": self.metric_ms(self.first_sentence_seen_at, first_audible),
            "tts_lock_to_play_ms": self.metric_ms(self.first_tts_lock_at, first_audible),
            "chunk_to_play_ms": self.metric_ms(self.first_chunk_generated_at, first_audible),
        }


def classify_mode_from_line(line: str, fallback: str) -> str:
    match = MODE_RE.search(line)
    if not match:
        return fallback
    graph = match.group("graph")
    sem = match.group("sem")
    if graph == "ON" and sem == "1":
        return "graph_serial"
    if graph == "OFF" and sem == "2":
        return "parallel"
    return f"graph_{graph.lower()}_sem_{sem}"


def should_mark_first_play(line: str) -> bool:
    if "[PLAYBACK-S1流式] 首声开始播放:" in line:
        return True
    if "[PLAYBACK-S1流式] 首个chunk开始物理播放:" in line:
        return True
    if "👄 [监控] 播放器开始物理播放和口型同步:" in line and sentence_seq(line) == 1:
        return True
    if "[PLAYBACK] 接收到首个音频块并开始播放:" in line and sentence_seq(line) == 1:
        return True
    return False


def iter_turns(lines: Iterable[str], source: str) -> list[TurnMetrics]:
    turns: list[TurnMetrics] = []
    current_mode = "unknown"
    current: TurnMetrics | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        ts = parse_log_time(line)
        current_mode = classify_mode_from_line(line, current_mode)

        if "🚀 新一轮对话开始" in line:
            if current is not None:
                turns.append(current)
            current = TurnMetrics(
                source=source,
                mode=current_mode,
                round_index=len(turns) + 1,
            )
            continue

        if current is None:
            continue

        if "Sending streaming API request to bedrock" in line and current.request_sent_at is None:
            current.request_sent_at = ts
            continue

        if "⏰ API调用开始时间" in line and current.api_start_at is None:
            current.api_start_at = ts
            continue

        if "⏩ Adding sentence to queue:" in line and sentence_seq(line) == 1 and current.first_sentence_seen_at is None:
            current.first_sentence_seen_at = ts
            continue

        if "[Graph Serial] 已获取锁，开始串行推理:" in line and sentence_seq(line) == 1 and current.first_tts_lock_at is None:
            current.first_tts_lock_at = ts
            continue

        if "[TTS-CHUNK] 首个音频块生成" in line and sentence_seq(line) == 1 and current.first_chunk_generated_at is None:
            current.first_chunk_generated_at = ts
            continue

        if should_mark_first_play(line):
            if current.first_play_at is None:
                current.first_play_at = ts
            if "物理播放和口型同步" in line and current.first_physical_play_at is None:
                current.first_physical_play_at = ts
            continue

    if current is not None:
        turns.append(current)
    return turns


def summarize(rows: list[dict], metric_key: str) -> tuple[int, float | None, float | None, float | None]:
    values = [row[metric_key] for row in rows if row[metric_key] is not None]
    if not values:
        return 0, None, None, None
    values.sort()
    median = statistics.median(values)
    avg = statistics.fmean(values)
    p90_index = max(0, min(len(values) - 1, int(round((len(values) - 1) * 0.9))))
    p90 = values[p90_index]
    return len(values), avg, median, p90


def format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def print_group_summary(rows: list[dict], group_name: str) -> None:
    count, avg, median, p90 = summarize(rows, "send_to_first_play_ms")
    print(f"\n## {group_name}")
    print(f"rounds={count} avg={format_ms(avg)}ms median={format_ms(median)}ms p90={format_ms(p90)}ms")
    print("round  mode          send->play  send->chunk  tts->play  chunk->play")
    for row in rows:
        print(
            f"{row['round']:>5}  "
            f"{row['mode']:<12}  "
            f"{format_ms(row['send_to_first_play_ms']):>10}  "
            f"{format_ms(row['send_to_first_chunk_ms']):>11}  "
            f"{format_ms(row['tts_lock_to_play_ms']):>9}  "
            f"{format_ms(row['chunk_to_play_ms']):>11}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse app logs and report end-to-end first-sentence latency for Bedrock runs."
    )
    parser.add_argument(
        "logs",
        nargs="+",
        help="One or more log files. You can mix current-mode and parallel-mode logs.",
    )
    args = parser.parse_args()

    all_rows: list[dict] = []
    for log_path in args.logs:
        path = Path(log_path)
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            turns = iter_turns(f, source=path.name)
        all_rows.extend(turn.as_row() for turn in turns if turn.start_time() is not None)

    if not all_rows:
        print("No Bedrock rounds with parseable start timestamps were found.")
        return

    print_group_summary(all_rows, "All Rounds")

    by_mode: dict[str, list[dict]] = {}
    for row in all_rows:
        by_mode.setdefault(row["mode"], []).append(row)
    if len(by_mode) > 1:
        for mode_name, rows in sorted(by_mode.items()):
            print_group_summary(rows, f"Mode: {mode_name}")


if __name__ == "__main__":
    main()
