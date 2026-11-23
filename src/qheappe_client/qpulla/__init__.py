"""Pulse-level access (IQM Pulla) through HEAppE/QScheduler.

Needs the IQM 36 stack (``qaas[iqm_qiskit]``). The names are imported on first
use, so ``qheappe_client`` still imports with another IQM stack installed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .heappe_iqm import HEAppEExecutor, HEAppEStorage
    from .qpulla import QPulla

__all__ = ["HEAppEExecutor", "HEAppEStorage", "QPulla"]

_MODULE_OF = {
    "QPulla": ".qpulla",
    "HEAppEExecutor": ".heappe_iqm",
    "HEAppEStorage": ".heappe_iqm",
}


def __getattr__(name: str) -> Any:
    module = _MODULE_OF.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        value = getattr(importlib.import_module(module, __name__), name)
    except ImportError as e:
        from ..iqm_compat.stack import IQMStack, install_hint

        raise ImportError(
            f"QPulla needs the IQM 36 stack (iqm-client 36.x, iqm-pulla 15.x): {e}. "
            f"Install it with: {install_hint(IQMStack.IQM36)}",
            name=e.name,
        ) from e
    globals()[name] = value
    return value
