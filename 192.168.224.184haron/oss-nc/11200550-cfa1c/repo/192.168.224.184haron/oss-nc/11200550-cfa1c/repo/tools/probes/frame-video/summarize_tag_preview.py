"""Summarize public-runtime pending-tag experiments; raw runs stay immutable."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from summarize_memory import summarize as memory
from summarize_product import summarize as product


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def summarize(run):
    meta = json.loads((run / "metadata.json").read_text())
    trace = json.loads((run / "tag-preview-trace.json").read_text())
    if not meta["options"].get("tagPreview") or trace["dropped"]:
        raise ValueError("Not a complete tag-preview trace")
    if not all(meta["triggerAvailability"].values()):
        raise ValueError("A requested runtime label was unavailable")
    m, p = memory(run), product(run)
    journey = [json.loads(row) for row in (run / "journey.ndjson").read_text().splitlines()]
    if any(row["status"] not in ("requested", "exercised") for row in journey):
        raise ValueError("A requested action was not exercised")
    events = [json.loads(row) for row in (run / "events.ndjson").read_text().splitlines()]
    phases = {row["phase"] for row in meta["journey"]}
    misses = [row for row in events if row["kind"] == "miss" and row["phase"] in phases]
    spans = [row for row in trace["rows"] if row["kind"] == "miss-span"]
    nodes = [row["after"]["label"] for row in trace["rows"] if row["kind"] == "node"]
    calls = [[row["method"], row["args"]] for row in trace["rows"] if row["kind"] == "runtime-call"]
    decisions = [[row["from"], row["to"]] for row in events if row["kind"] == "decision"]
    inputs = [{key: value for key, value in row.items() if key not in ("phase",)}
              for row in meta["journey"] if row["command"] != "tag-preview"]
    previews = [row for row in trace["rows"] if row["kind"] == "preview"]
    unchanged = all(row["before"] == row["after"] for row in trace["rows"] if row["kind"] in ("preview", "withdraw"))
    if not unchanged:
        raise ValueError("A preparation changed playback state")
    return dict(run=run.name, mode=meta["options"]["tagPreview"], seconds=m["seconds"],
                budgetMiB=m["textureBudgetMiB"], after20Seconds=m["after20Seconds"],
                cpuOneCorePercent=p["resources"]["all"]["totalCpuOneCorePercent"],
                validNativeSamples=m["validNativeSamples"], invalidNativeSamples=m["invalidNativeSamples"],
                journeyMisses=m["journeyMisses"], missesAfter20Seconds=m["missesAfter20Seconds"],
                missesByLabel=dict(Counter(row["label"] for row in misses)),
                missSpans=dict(count=len(spans), maxMs=max((row["ms"] for row in spans), default=0),
                               over50=sum(row["ms"] > 50 for row in spans),
                               longest=sorted(spans, key=lambda row: row["ms"], reverse=True)[:10]),
                previews=len(previews), previewTargetCounts=dict(Counter(len(row["labels"]) for row in previews)),
                unchangedDuringPreparation=unchanged,
                actualInputSha256=fingerprint(inputs), runtimeCallSha256=fingerprint(calls),
                nodeSequence=nodes, nodeSequenceSha256=fingerprint(nodes), decisions=len(decisions),
                decisionSequenceSha256=fingerprint(decisions), finalTextureStats=m["finalTextureStats"],
                finalVideo=m["finalVideo"], release=m["release"], sourceSha256=m["productSourceSha256"],
                rawSha256={name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                           for name in ("metadata.json", "summary.json", "tag-preview-trace.json", "journey.ndjson")},
                limitations=[trace["limitation"], "One machine and one run per mode; scheduling jitter can change natural graph timing.",
                             "Miss spans measure consecutive unsuccessful renderer targets, not physical screen presentation or voice latency.",
                             "All modes share scheduled public inputs, not forced nodes; inspect actual node/decision sequences before attributing resource differences."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.run)
    with (args.output or args.run / "tag-preview-summary.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps({key: result[key] for key in ("run", "cpuOneCorePercent", "journeyMisses", "missesAfter20Seconds", "missesByLabel", "missSpans", "previews", "nodeSequenceSha256")}))


if __name__ == "__main__":
    main()
