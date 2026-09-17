"""SDLC Manager agent package interface."""

import importlib
from types import ModuleType

__all__ = ["agent"]

try:
    from ._eval_app_aware_patch import apply_app_aware_eval_patch

    apply_app_aware_eval_patch()
except ModuleNotFoundError:
    pass


def __getattr__(name: str) -> ModuleType:
    """Lazy load agent module when accessed."""
    if name in __all__:
        return importlib.import_module("." + name, __package__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
