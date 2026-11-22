"""Unit hooks shared by the build-time tracer and the run-time slicer.

A *unit* is an outermost `self.subTest(...)` block or an outermost Validator.validate_identity /
validate_all / validate_transpile call inside a test method. Units are numbered 0, 1, ... in the
order they start within one test method; nested unit events (e.g. validate_all's own subTests)
belong to the enclosing unit.

trace mode: record which sqlglot lines each unit executes (sys.monitoring LINE, once per unit).
slice mode: run only the units listed for the current test; others are skipped.
"""
import contextlib
import sys
import unittest

STATE = {"depth": 0, "n": 0, "keep": None, "on_enter": None, "on_exit": None}


class _Null:
    def __getattr__(self, name):
        return self

    def __call__(self, *a, **k):
        return self

    def __iter__(self):
        return iter(())

    def __getitem__(self, k):
        return self


NULL = _Null()


class _SkipBody(Exception):
    pass


class _Skipper:
    def __enter__(self):
        f = sys._getframe(1)
        self._old = sys.gettrace()
        sys.settrace(lambda *a, **k: None)
        f.f_trace_lines = True
        f.f_trace = self._tr
        self._f = f
        return None

    def _tr(self, frame, event, arg):
        if event == "line":
            raise _SkipBody
        return self._tr

    def __exit__(self, t, v, tb):
        self._f.f_trace = None
        sys.settrace(self._old)
        return t is _SkipBody


def reset():
    STATE["depth"] = 0
    STATE["n"] = 0


def _begin():
    """Returns (counted_ordinal_or_None, skip)."""
    if STATE["depth"] > 0:
        STATE["depth"] += 1
        return None, False
    k = STATE["n"]
    STATE["n"] += 1
    keep = STATE["keep"]
    if keep is not None and k not in keep:
        return k, True
    STATE["depth"] += 1
    if STATE["on_enter"]:
        STATE["on_enter"](k)
    return k, False


def _end(k):
    STATE["depth"] -= 1
    if k is not None and STATE["depth"] == 0 and STATE["on_exit"]:
        STATE["on_exit"](k)


_orig_subtest = unittest.TestCase.subTest


def _subtest(self, *a, **k):
    o, skip = _begin()
    if skip:
        return _Skipper()
    return _wrap_cm(_orig_subtest(self, *a, **k), o)


@contextlib.contextmanager
def _wrap_cm(cm, o):
    try:
        with cm:
            yield
    finally:
        _end(o)


def _wrap_validator(fn):
    def w(self, *a, **k):
        o, skip = _begin()
        if skip:
            # the result is sometimes chained on (".selects[0]"): hand back the parsed expression,
            # asserting nothing; lines run here are not assertions so they cannot hide a failure
            try:
                return self.parse_one(a[0]) if a else NULL
            except Exception:  # noqa: BLE001
                return NULL
        try:
            return fn(self, *a, **k)
        finally:
            _end(o)
    w.__wrapped__ = fn
    return w


def install():
    unittest.TestCase.subTest = _subtest
    try:
        from tests.dialects import test_dialect as td
    except Exception:  # noqa: BLE001
        return
    V = td.Validator
    for name in ("validate_identity", "validate_all", "validate_transpile"):
        f = V.__dict__.get(name)
        if f is not None and not hasattr(f, "__wrapped__"):
            setattr(V, name, _wrap_validator(f))
