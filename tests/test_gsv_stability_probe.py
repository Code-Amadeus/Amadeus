from __future__ import annotations

from tools.probes.probe_gsv_stability import (
    _longest_equal_run,
    _mark_anomalies,
    _periodic_run,
    _token_metrics,
)


def test_token_metrics_detect_equal_and_periodic_runs() -> None:
    tokens = [1, 2, 1, 2, 1, 2, 7, 7, 7, 7]
    metrics = _token_metrics(tokens)

    assert _longest_equal_run(tokens) == 4
    assert _periodic_run(tokens, max_period=2) == (6, 2)
    assert metrics["longest_equal_run"] == 4
    assert metrics["longest_periodic_run"] == 6
    assert metrics["periodic_run_period"] == 2


def test_mark_anomalies_uses_case_relative_length_and_stop_reason() -> None:
    rows = []
    for seed, length in enumerate([20, 21, 22, 140], start=1):
        rows.append(
            {
                "case": "filler",
                "mode": "dynamic",
                "seed": seed,
                "termination": "eos",
                "token_count": length,
                "longest_equal_run": 1,
                "longest_periodic_run": 2,
                "top_token_ratio": 0.1,
            }
        )
    rows.append(
        {
            "case": "filler",
            "mode": "static",
            "seed": 5,
            "termination": "early_stop",
            "token_count": 60,
            "longest_equal_run": 1,
            "longest_periodic_run": 2,
            "top_token_ratio": 0.1,
        }
    )

    _mark_anomalies(rows)

    assert rows[0]["anomalous"] is False
    assert rows[3]["anomaly_reasons"] == ["length_outlier"]
    assert rows[4]["anomaly_reasons"] == ["early_stop"]
