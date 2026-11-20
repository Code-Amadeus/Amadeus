"""List and test microphone devices used by ASR/WakeService.

Examples:
    python tools/list_microphones.py
    python tools/list_microphones.py --monitor 6 --seconds 8
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.microphone import choose_microphone, list_microphones, monitor_microphone


def main() -> int:
    parser = argparse.ArgumentParser(description="List and test Amadeus microphone devices")
    parser.add_argument("--sample-seconds", type=float, default=0.6)
    parser.add_argument("--monitor", type=int, default=None, help="Monitor one device index and print live RMS")
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()

    devices = list_microphones(sample_seconds=args.sample_seconds)
    chosen = choose_microphone(devices=devices, log=False)
    print("index | selected | rms | virtual | preferred | host | name")
    print("-" * 96)
    for device in devices:
        selected = "*" if chosen and device.index == chosen.index else " "
        print(
            f"{device.index:>5} | {selected:^8} | {device.rms:>5} | "
            f"{str(device.is_virtual):>7} | {str(device.preferred):>9} | "
            f"{device.host_api} | {device.name}"
        )
    if chosen is None:
        print("\nNo input microphone was selected.")
    else:
        print(f"\nSelected: [{chosen.index}] {chosen.name} ({chosen.reason}, rms={chosen.rms})")

    if args.monitor is not None:
        print(f"\nMonitoring [{args.monitor}] for {args.seconds:.1f}s. Speak now; RMS should rise clearly.")
        monitor_microphone(args.monitor, seconds=args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
