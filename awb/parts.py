"""A module split into parts stays the door to them (F1 step 6: buildrun.py and hooks.py; step 8: the intake).

`join(name, *parts)` gives the old module every name of its loaded parts that it does not bind itself, so an old
import path (`from awb.hooks import guard`, `buildrun.Host`) keeps working. It also makes a name set on the old
module reach each part that holds the same object: a test that replaces `hooks.place_class` replaces the one the
guard calls; the undo puts both back. A part that is not loaded yet is still named: it is reached once it is.
A part that imports the old module itself (the runner's selftest) is joined again when the old module first loads
it.
"""
from __future__ import annotations

import sys
import types

_NONE = object()


class _Door(types.ModuleType):
    """The old module: a set name goes on to every part that holds the object it replaces."""

    def __setattr__(self, name: str, value) -> None:
        old = self.__dict__.get(name, _NONE)
        if old is not _NONE:
            for part in self.__dict__.get("_parts_", ()):
                module = sys.modules.get(part)
                if module is not None and module.__dict__.get(name, _NONE) is old:
                    setattr(module, name, value)
        super().__setattr__(name, value)


def join(name: str, *parts: str, classes: bool = False) -> None:
    """Make the module `name` the door to the modules `parts` (full names). With `classes` a class of a part names
    `name` as its module, as it did before the split (F1 step 8: its repr, its pickle path and a look over the classes
    of the old module stay as they were)."""
    module = sys.modules[name]
    known = module.__dict__.get("_parts_", ())
    module.__dict__["_parts_"] = known + tuple(p for p in parts if p not in known)
    for part in parts:
        loaded = sys.modules.get(part)
        for key, value in (vars(loaded).items() if loaded is not None else ()):
            if not key.startswith("__"):
                module.__dict__.setdefault(key, value)
                if classes and isinstance(value, type) and value.__module__ == part:
                    value.__module__ = name
    module.__class__ = _Door
