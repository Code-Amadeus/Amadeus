"""Numeric receipts only, grouped under an admitted measurement turn."""
from threading import Lock
from typing import Any


def _bounded_integer(value: Any, limit: int | None = None) -> int | None:
    if type(value) is int and value >= 0 and (limit is None or value <= limit):
        return value
    return None


def token_usage(raw: Any) -> dict[str, int | None] | None:
    prompt = _bounded_integer(getattr(raw, "prompt_tokens", None))
    completion = _bounded_integer(getattr(raw, "completion_tokens", None))
    total = _bounded_integer(getattr(raw, "total_tokens", None))
    if prompt is None or completion is None or total != prompt + completion:
        return None
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    completion_details = getattr(raw, "completion_tokens_details", None)
    return {
        "input_tokens": prompt,
        "cached_input_tokens": _bounded_integer(getattr(prompt_details, "cached_tokens", None), prompt),
        "output_tokens": completion,
        "reasoning_tokens": _bounded_integer(getattr(completion_details, "reasoning_tokens", None), completion),
    }


class RoleUsageReceipt:
    """Count starts and reconcile numeric usage irrespective of draft selection."""

    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")

    def __init__(self, turn_id: str):
        if not turn_id:
            raise ValueError("observation requires an admitted turn")
        self.turn_id = turn_id
        self._requests = {}
        self._lock = Lock()

    def observe(self, event: dict) -> None:
        if event["turn_id"] != self.turn_id:
            raise ValueError("provider receipt belongs to another turn")
        request = event["request_id"]
        if not request:
            raise ValueError("provider receipt has no request identity")
        with self._lock:
            if event["kind"] == "started":
                if request in self._requests:
                    raise ValueError("duplicate request start")
                self._requests[request] = {"provider": event["provider"], "model": event["model"],
                                           "usage": None, "conflict": False}
                return
            if event["kind"] != "usage":
                raise ValueError("unknown provider receipt event")
            row = self._requests[request]
            if (event["provider"], event["model"]) != (row["provider"], row["model"]):
                raise ValueError("provider receipt identity changed")
            usage = event["usage"]
            if row["conflict"]:
                return
            if row["usage"] is None:
                row["usage"] = dict(usage)
            elif row["usage"] != usage:
                row["usage"] = None
                row["conflict"] = True

    def report(self) -> dict:
        with self._lock:
            rows = [{"request_id": request, **item} for request, item in self._requests.items()]
        accounted = [row["usage"] for row in rows if row["usage"] is not None]
        totals = {}
        known = {}
        for field in self.fields:
            values = [tokens[field] for tokens in accounted if tokens[field] is not None]
            known[field] = sum(values)
            totals[field] = known[field] if len(values) == len(rows) else None
        return {"turn_id": self.turn_id, "attempts": len(rows), "known": len(accounted),
                "unknown": len(rows) - len(accounted), "usage": totals,
                "known_usage": known, "requests": rows}


def paired_usage(baseline: dict, candidate: dict) -> dict:
    delta = {}
    a, b = baseline["usage"], candidate["usage"]
    for field in RoleUsageReceipt.fields:
        delta[field] = b[field] - a[field] if a[field] is not None and b[field] is not None else None
    return {"additional_requests": candidate["attempts"] - baseline["attempts"], "delta": delta,
            "unknown_requests": (baseline["unknown"], candidate["unknown"])}
