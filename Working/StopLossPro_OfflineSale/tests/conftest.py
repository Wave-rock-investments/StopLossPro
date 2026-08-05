"""Headless test harness for the StopLossPro risk engine.

`lib/constants.py` imports `kivy.utils.platform` and (lazily) `kivy.metrics.dp`.
Kivy needs a display and is heavy to install in CI, and the risk engine itself
does not use either symbol. Rather than modify production code to make it
testable — which would violate the "do not touch the risk engine" rule — we
install a minimal stub for `kivy` in `sys.modules` BEFORE `constants` is
imported.

This keeps `lib/` byte-for-byte unmodified while allowing the risk maths to be
regression-tested on any machine, including a headless build server.
"""
import os
import sys
import types

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


def _install_kivy_stub() -> None:
    if "kivy" in sys.modules:
        return

    kivy = types.ModuleType("kivy")

    utils = types.ModuleType("kivy.utils")
    utils.platform = "win"

    metrics = types.ModuleType("kivy.metrics")
    metrics.dp = lambda v: v          # identity: density scaling is irrelevant to the maths

    kivy.utils = utils
    kivy.metrics = metrics

    sys.modules["kivy"] = kivy
    sys.modules["kivy.utils"] = utils
    sys.modules["kivy.metrics"] = metrics


_install_kivy_stub()
