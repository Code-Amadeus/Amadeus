"""VN Player runtime package.

This package keeps the visual novel mode separate from the ordinary chat path:
game text is observed, indexed, logged, and then optionally rendered as a
Kurisu reaction.
"""

from .runtime import VNPlayerRuntime

__all__ = ["VNPlayerRuntime"]
