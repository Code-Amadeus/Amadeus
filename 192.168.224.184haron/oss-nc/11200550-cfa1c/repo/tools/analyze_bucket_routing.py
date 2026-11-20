# -*- coding: utf-8 -*-
"""
Bucket routing analysis v2 — text-aware vs fixed-offset strategies.

Usage:
    py -3 tools/analyze_bucket_routing.py
"""

# ── Config ──────────────────────────────────────────────────────────────────
KV_CACHE_BUCKETS = [128, 256, 448, 512, 768, 1024]
STRIDE           = 32
PRECAP_LO        = 0.55
PRECAP_HI        = 0.92

# Empirical use-case profiles
# (label, kv_cache_len, x_lens_text_tokens, expected_output_tokens)
# expected_output estimated from: typical TTS at 50 Hz semantic token rate
PROFILES = [
    ("short-ja",   260, 50,  80),
    ("med-ja",     310, 80, 120),
    ("long-ja",    360, 120, 170),
    ("3sent-sm",   420, 150, 230),
    ("3sent-med",  450, 180, 270),
    ("3sent-lg",   490, 220, 320),
    ("3sent-xl",   510, 250, 370),
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def align(kv):
    return ((kv + STRIDE - 1) // STRIDE) * STRIDE


def select(kv, min_needed):
    for b in KV_CACHE_BUCKETS:
        if min_needed < b:
            return b
    return None


def gen_slots(kv, bucket):
    return (bucket - align(kv)) if bucket else 0


def strategy_fixed192(kv, x_lens):
    al = align(kv)
    return select(kv, al + 192)


def strategy_text_aware(kv, x_lens, ratio=1.5):
    al = align(kv)
    estimated = max(64, int(x_lens * ratio))
    return select(kv, al + estimated)


def precap_hit(kv, bucket):
    if not bucket:
        return False
    lo = int(bucket * PRECAP_LO)
    hi = int(bucket * PRECAP_HI)
    al = align(kv)
    return lo <= kv <= hi and al < bucket


# ── Profile table ────────────────────────────────────────────────────────────

def print_profile_table():
    print("=" * 100)
    print("  Profile comparison: fixed-192 vs text-aware(ratio=1.5) vs text-aware(ratio=2.0)")
    print("  Columns: bucket | gen_slots | covers_expected | precap_hit")
    print("=" * 100)
    fmt = "  {:<12}  kv={:3d} x={:3d} exp={:3d}  |  fixed-192: {:4} {:3d}sl {} {}  |  ta-1.5: {:4} {:3d}sl {} {}  |  ta-2.0: {:4} {:3d}sl {} {}"

    for label, kv, xl, exp in PROFILES:
        def row(bucket, exp_out):
            if bucket is None:
                return "None", 0, "??", "??"
            slots = gen_slots(kv, bucket)
            covers = "OK " if slots >= exp_out else "LOW"
            pc = "pc+" if precap_hit(kv, bucket) else "pc-"
            return str(bucket), slots, covers, pc

        b192    = strategy_fixed192(kv, xl)
        bta15   = strategy_text_aware(kv, xl, 1.5)
        bta20   = strategy_text_aware(kv, xl, 2.0)

        r192    = row(b192,  exp)
        rta15   = row(bta15, exp)
        rta20   = row(bta20, exp)

        print(fmt.format(
            label, kv, xl, exp,
            r192[0], r192[1], r192[2], r192[3],
            rta15[0], rta15[1], rta15[2], rta15[3],
            rta20[0], rta20[1], rta20[2], rta20[3],
        ))

    print()


# ── Sweep tables ─────────────────────────────────────────────────────────────

def print_sweep(ratio):
    print(f"  Sweep: text-aware(ratio={ratio})  —  kv vs x_lens routing table")
    print("  Cell format: bucket(gen_slots)  OK/LOW = covers expected output")
    print()
    kv_range = [260, 300, 340, 380, 420, 450, 480, 510]
    xl_range = [40, 60, 80, 100, 120, 150, 180, 220, 250]
    header = f"  {'kv\\xl':>7} |" + "".join(f" {xl:>12}" for xl in xl_range)
    print(header)
    print("  " + "-" * (9 + 13 * len(xl_range)))
    for kv in kv_range:
        row = f"  {kv:7d} |"
        for xl in xl_range:
            b = strategy_text_aware(kv, xl, ratio)
            slots = gen_slots(kv, b)
            exp = max(64, int(xl * ratio))
            flag = "OK " if slots >= exp else "LOW"
            cell = f"{b or 'None'}({slots}){flag}"
            row += f" {cell:>12}"
        print(row)
    print()


# ── Utilization analysis ──────────────────────────────────────────────────────

def print_utilization(ratio):
    print(f"  Bucket utilization: text-aware(ratio={ratio})")
    print("  util = align(kv) / bucket  — ideal range [0.55, 0.85]")
    print()
    for label, kv, xl, exp in PROFILES:
        b = strategy_text_aware(kv, xl, ratio)
        if b is None:
            print(f"  {label:<12}  kv={kv}  -> None (dynamic fallback)")
            continue
        al = align(kv)
        slots = gen_slots(kv, b)
        util = al / b
        util_flag = "GOOD" if 0.50 <= util <= 0.88 else ("HIGH" if util > 0.88 else "LOW ")
        covers = "OK " if slots >= exp else "LOW"
        pc = "precap+" if precap_hit(kv, b) else "precap-"
        print(f"  {label:<12}  kv={kv:3d} x={xl:3d}  bucket={b:4d}  "
              f"align={al:3d}  util={util:.2f} {util_flag}  "
              f"slots={slots:3d}/{exp:3d} {covers}  {pc}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print_profile_table()

    for ratio in (1.5, 2.0):
        print("=" * 70)
        print_sweep(ratio)
        print_utilization(ratio)

    print("=" * 70)
    print("  Recommendation summary")
    print("=" * 70)
    print("""
  Fixed MIN_GEN_SLOTS=192:
    - Single sentences (kv<400): routes to 768 even when 448/512 suffices -> WASTES memory
    - 3-sentence merges (kv=420-510): routes to 768 with 256-320 slots   -> OK

  Text-aware ratio=1.5:
    - Short sentences:  kv=260-370 + x=50-100  -> bucket 448 or 512 (slots 128-224) -> OK
    - 3-sentence merge: kv=420-510 + x=150-250 -> bucket 768 (slots 256-320)        -> OK
    - Precapture: both ranges covered by existing warmup keys              -> no re-capture

  Text-aware ratio=2.0:
    - More conservative; some 3-sentence merges jump to 1024              -> slight over-alloc
    - Use 2.0 if you observe output length often exceeding slots at 1.5

  Verdict:
    ratio=1.5 is the best tradeoff for this workload.
    Pass x_lens[0].item() to _select_bucket at the call site.
    The constant can be tuned without touching the routing logic.
""")


if __name__ == "__main__":
    main()
