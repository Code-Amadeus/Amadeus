"""Parse main app logs and report first-sentence E2E latency breakdown.

Preferred input is a fresh log produced after the ASCII [LATENCY] markers were
added. The script still has a small fallback for older logs, but the most
precise path is:

    .\\.venv\\Scripts\\python.exe -X utf8 main.py 2>&1 | Tee-Object run.log
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\e2e_chain_report.py run.log
"""

from __future__ import annotations

import argparse
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

ROUND_RE = re.compile(r"新一轮对话开始")
MODE_RE = re.compile(r"\[TTS Mode\].*CUDA_Graph=(?P<graph>ON|OFF), semaphore=(?P<sem>\d+)")
LAT_RE = re.compile(r"\[LATENCY\]\s+stage=(?P<stage>[a-zA-Z0-9_]+)\s+ms=(?P<ms>\d+(?:\.\d+)?)(?:\s+(?P<rest>.*))?$")
INLINE_PLAY_RE = re.compile(r"API发送→首声开始:\s*(?P<ms>\d+(?:\.\d+)?)ms")


def classify_mode(line: str, fallback: str) -> str:
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


@dataclass
class Turn:
    source: str
    round_index: int
    mode: str = "unknown"
    request_sent: float | None = None
    bedrock_submit: float | None = None
    bedrock_rpc_start: float | None = None
    stream_established: float | None = None
    early_cut: float | None = None
    first_sentence_enqueued: float | None = None
    first_sentence_dequeued: float | None = None
    first_graph_lock: float | None = None
    first_tts_enter: float | None = None
    first_chunk_generated: float | None = None
    first_play: float | None = None

    def stage(self, a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return b - a

    def row(self) -> dict[str, object]:
        send_to_play = self.first_play
        send_to_submit = self.bedrock_submit
        submit_to_rpc = self.stage(self.bedrock_submit, self.bedrock_rpc_start)
        rpc_to_stream = self.stage(self.bedrock_rpc_start, self.stream_established)
        send_to_stream = self.stream_established
        send_to_enqueue = self.first_sentence_enqueued
        enqueue_to_dequeue = self.stage(self.first_sentence_enqueued, self.first_sentence_dequeued)
        dequeue_to_lock = self.stage(self.first_sentence_dequeued, self.first_graph_lock)
        lock_to_chunk = self.stage(self.first_graph_lock, self.first_chunk_generated)
        chunk_to_play = self.stage(self.first_chunk_generated, self.first_play)

        candidates = {
            "send->submit": send_to_submit,
            "submit->rpc": submit_to_rpc,
            "rpc->stream": rpc_to_stream,
            "send->stream": send_to_stream,
            "stream->enqueue": self.stage(self.stream_established, self.first_sentence_enqueued),
            "enqueue->dequeue": enqueue_to_dequeue,
            "dequeue->graph_lock": dequeue_to_lock,
            "graph_lock->chunk": lock_to_chunk,
            "chunk->play": chunk_to_play,
        }
        valid = [(k, v) for k, v in candidates.items() if v is not None]
        bottleneck_name, bottleneck_ms = ("-", None) if not valid else max(valid, key=lambda item: item[1])

        return {
            "source": self.source,
            "round": self.round_index,
            "mode": self.mode,
            "send_to_play": send_to_play,
            "send_to_submit": send_to_submit,
            "submit_to_rpc": submit_to_rpc,
            "rpc_to_stream": rpc_to_stream,
            "send_to_stream": send_to_stream,
            "send_to_enqueue": send_to_enqueue,
            "enqueue_to_dequeue": enqueue_to_dequeue,
            "dequeue_to_lock": dequeue_to_lock,
            "lock_to_chunk": lock_to_chunk,
            "chunk_to_play": chunk_to_play,
            "bottleneck_name": bottleneck_name,
            "bottleneck_ms": bottleneck_ms,
        }


def parse_latency_stage(line: str) -> tuple[str, float] | None:
    match = LAT_RE.search(line)
    if not match:
        return None
    return match.group("stage"), float(match.group("ms"))


def parse_lines(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current_mode = "unknown"
    current: Turn | None = None

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            current_mode = classify_mode(line, current_mode)

            if ROUND_RE.search(line):
                if current is not None:
                    rows.append(current.row())
                current = Turn(source=path.name, round_index=len(rows) + 1, mode=current_mode)
                continue

            if current is None:
                continue

            current.mode = current_mode

            stage_info = parse_latency_stage(line)
            if stage_info:
                stage, ms = stage_info
                if stage == "request_sent" and current.request_sent is None:
                    current.request_sent = ms
                elif stage == "bedrock_submit" and current.bedrock_submit is None:
                    current.bedrock_submit = ms
                elif stage == "bedrock_rpc_start" and current.bedrock_rpc_start is None:
                    current.bedrock_rpc_start = ms
                elif stage == "stream_established" and current.stream_established is None:
                    current.stream_established = ms
                elif stage == "first_sentence_early_cut" and current.early_cut is None:
                    current.early_cut = ms
                elif stage == "first_sentence_enqueued" and current.first_sentence_enqueued is None:
                    current.first_sentence_enqueued = ms
                elif stage == "first_sentence_dequeued" and current.first_sentence_dequeued is None:
                    current.first_sentence_dequeued = ms
                elif stage == "first_graph_lock" and current.first_graph_lock is None:
                    current.first_graph_lock = ms
                elif stage == "first_tts_enter" and current.first_tts_enter is None:
                    current.first_tts_enter = ms
                elif stage == "first_chunk_generated" and current.first_chunk_generated is None:
                    current.first_chunk_generated = ms
                elif stage == "first_play" and current.first_play is None:
                    current.first_play = ms
                continue

            # Fallback for older logs with inline E2E only.
            inline = INLINE_PLAY_RE.search(line)
            if inline and current.first_play is None:
                current.first_play = float(inline.group("ms"))

    if current is not None:
        rows.append(current.row())
    return rows


def fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def summarize(rows: list[dict[str, object]], key: str) -> tuple[int, float | None, float | None]:
    values = [float(row[key]) for row in rows if isinstance(row[key], (int, float, float))]
    if not values:
        return 0, None, None
    return len(values), statistics.fmean(values), statistics.median(values)


def print_rows(rows: list[dict[str, object]], title: str) -> None:
    print(f"\n## {title}")
    print(
        "round  mode          send->play  send->submit  submit->rpc  rpc->stream  "
        "send->stream  send->enqueue  enqueue->deq  deq->lock  lock->chunk  chunk->play  bottleneck"
    )
    for row in rows:
        print(
            f"{row['round']:>5}  "
            f"{str(row['mode']):<12}  "
            f"{fmt_ms(row['send_to_play']):>10}  "
            f"{fmt_ms(row['send_to_submit']):>12}  "
            f"{fmt_ms(row['submit_to_rpc']):>11}  "
            f"{fmt_ms(row['rpc_to_stream']):>11}  "
            f"{fmt_ms(row['send_to_stream']):>12}  "
            f"{fmt_ms(row['send_to_enqueue']):>13}  "
            f"{fmt_ms(row['enqueue_to_dequeue']):>12}  "
            f"{fmt_ms(row['dequeue_to_lock']):>9}  "
            f"{fmt_ms(row['lock_to_chunk']):>11}  "
            f"{fmt_ms(row['chunk_to_play']):>11}  "
            f"{row['bottleneck_name']}({fmt_ms(row['bottleneck_ms'])}ms)"
        )


def print_summary(rows: list[dict[str, object]]) -> None:
    print("\nStage Medians")
    for key, label in [
        ("send_to_submit", "send->submit"),
        ("submit_to_rpc", "submit->rpc"),
        ("rpc_to_stream", "rpc->stream"),
        ("send_to_stream", "send->stream"),
        ("send_to_enqueue", "send->enqueue"),
        ("enqueue_to_dequeue", "enqueue->dequeue"),
        ("dequeue_to_lock", "dequeue->graph_lock"),
        ("lock_to_chunk", "graph_lock->chunk"),
        ("chunk_to_play", "chunk->play"),
        ("send_to_play", "send->play"),
    ]:
        count, avg, median = summarize(rows, key)
        print(f"{label:<20} count={count:<3} avg={fmt_ms(avg):>8}ms median={fmt_ms(median):>8}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report first-sentence E2E latency from app logs.")
    parser.add_argument("logs", nargs="+", help="One or more UTF-8 log files captured from main.py.")
    args = parser.parse_args()

    all_rows: list[dict[str, object]] = []
    for log in args.logs:
        all_rows.extend(parse_lines(Path(log)))

    all_rows = [row for row in all_rows if row["send_to_play"] is not None]
    if not all_rows:
        raise SystemExit("No parseable rounds found. Capture a fresh log after the [LATENCY] markers are present.")

    print_rows(all_rows, "All Rounds")
    print_summary(all_rows)

    by_mode: dict[str, list[dict[str, object]]] = {}
    for row in all_rows:
        by_mode.setdefault(str(row["mode"]), []).append(row)
    if len(by_mode) > 1:
        for mode, rows in sorted(by_mode.items()):
            print_rows(rows, f"Mode: {mode}")
            print_summary(rows)


if __name__ == "__main__":
    main()
