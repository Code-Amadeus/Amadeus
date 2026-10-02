"""Companion context composition for chat, Galgame and reading."""

from .context import CompanionContextBuilder
from .runtime import CompanionRuntime
from .scenes import SceneBinding, SceneStore

__all__ = ["CompanionContextBuilder", "CompanionRuntime", "SceneBinding", "SceneStore"]