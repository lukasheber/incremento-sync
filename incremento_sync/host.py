"""Finding the Incremento add-on that is loaded beside this one."""

from __future__ import annotations

import os
import sys
from types import ModuleType

_MARKERS = (("backend", "db_schema.py"), ("backend", "priority_manager.py"))


def find_incremento() -> ModuleType | None:
    """The root module of the loaded Incremento add-on (recognised by its backend files), or None."""
    own = __name__.split(".")[0]
    for name, module in list(sys.modules.items()):
        if "." in name or name == own or module is None:
            continue
        path = getattr(module, "__file__", None)
        if not path:
            continue
        folder = os.path.dirname(path)
        if all(os.path.isfile(os.path.join(folder, *marker)) for marker in _MARKERS):
            return module
    return None
