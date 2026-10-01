"""Shared EMO presets; safe to import without settings or model dependencies."""

# Duration ranges declared in the shipping role prompts. serious_speaking is
# defined by its semantic span instead of a numeric duration.
EMOTION_DURATION_RANGES: dict[str, tuple[float, float] | None] = {
    "normal": (2.0, 6.0),
    "thinking": (10.0, 15.0),
    "smile": (1.0, 2.0),
    "happy": (1.0, 2.0),
    "shy": (2.0, 4.0),
    "blush": (2.0, 4.0),
    "angry": (3.0, 5.0),
    "sad": (3.0, 5.0),
    "disappointed": (3.0, 5.0),
    "surprised": (1.0, 2.0),
    "serious_speaking": None,
}
