"""Microsoft Teams channel for xPerfect Experts.

The package speaks the Teams protocol through Microsoft's Teams SDK and talks to the
xPerfect core only through its channel routes. It never decides what a message means.
"""
from .bridge import Bridge, RuntimeClient

__all__ = ["Bridge", "RuntimeClient"]
