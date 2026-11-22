"""Unit hooks shared by the build-time tracer and the run-time slicer.

A *unit* is one of:
  ("r", k)        the k-th outermost `self.subTest(...)` block or Validator.validate_identity /
                  validate_all / validate_transpile call inside a test method (k = 0, 1, ... in start
                  order; nested unit events belong to the enclosing unit), or
  ("c", file, i)  fixture pair i of `TestOptimizer.check_file(file, ...)` called at the top level of a
                  test method: the pair's optimize job (normally run in a spawned process pool, here
                  run synchronously in-process) plus its compare subTest. check_file's own subTests are
                  not counted as ("r", k) units.

trace mode: on_enter/on_exit receive unit keys; the tracer records which sqlglot lines each unit runs.
slice mode: set_keep() names the units to run; other ("r") units are skipped and other check_file
pairs are never yielded to check_file.
"""
import contextlib
import pickle
import sys
import unittest
from concurrent.futures import Future

STATE = {"depth": 0, "n": 0, "keep": None, "cf_keep": None, "on_enter": None, "on_exit": None,
         "cf_active": False, "cf_file": None, "cf_cur": None, "cf_titles": None}


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
    STATE["cf_active"] = False
    STATE["cf_file"] = None
    STATE["cf_cur"] = None
    STATE["cf_titles"] = None


def set_keep(v):
    """v: None (run everything), a list of ("r") ordinals, or {"reg": [...], "cf": [[file, i], ...]}."""
    if v is None:
        STATE["keep"] = None
        STATE["cf_keep"] = None
    elif isinstance(v, dict):
        STATE["keep"] = set(v.get("reg") or ())
        STATE["cf_keep"] = {(f, int(i)) for f, i in (v.get("cf") or ())}
    else:
        STATE["keep"] = set(v)
        STATE["cf_keep"] = set()


def _enter(key):
    if STATE["on_enter"]:
        STATE["on_enter"](key)


def _exit(key):
    if STATE["on_exit"]:
        STATE["on_exit"](key)


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
    _enter(("r", k))
    return k, False


def _end(k):
    STATE["depth"] -= 1
    if k is not None and STATE["depth"] == 0:
        _exit(("r", k))


_orig_subtest = unittest.TestCase.subTest


def _subtest(self, *a, **k):
    if STATE["cf_active"] and STATE["depth"] == 0:
        # check_file's compare block for one pair: part of that pair's unit, never skipped here
        # (pairs that are not kept were never yielded)
        title = a[0] if a else k.get("msg")
        key = (STATE["cf_titles"] or {}).get(title) if isinstance(title, str) else None
        return _wrap_cf(_orig_subtest(self, *a, **k), key)
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


@contextlib.contextmanager
def _wrap_cf(cm, key):
    STATE["depth"] += 1
    if key is not None:
        _enter(("c",) + key)
    try:
        with cm:
            yield
    finally:
        STATE["depth"] -= 1
        if key is not None:
            _exit(("c",) + key)


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


class SyncPool:
    """Drop-in for the spawned ProcessPoolExecutor of TestOptimizer: runs each job at submit time,
    in-process, with the same pickle round trip of the result (and of a raised exception)."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        f = Future()
        key = STATE["cf_cur"] if STATE["cf_active"] and STATE["depth"] == 0 else None
        STATE["depth"] += 1
        if key is not None:
            _enter(("c",) + key)
        try:
            r = fn(*a, **k)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        finally:
            STATE["depth"] -= 1
            if key is not None:
                _exit(("c",) + key)
        return f

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wrap_check_file(fn):
    def w(self, file, *a, **k):
        if STATE["depth"] > 0 or STATE["cf_active"]:
            return fn(self, file, *a, **k)  # nested in a unit: belongs to it, unfiltered
        STATE["cf_active"] = True
        STATE["cf_file"] = file
        STATE["cf_titles"] = {}
        try:
            return fn(self, file, *a, **k)
        finally:
            STATE["cf_active"] = False
            STATE["cf_file"] = None
            STATE["cf_cur"] = None
            STATE["cf_titles"] = None
    w.__wrapped__ = fn
    return w


def _wrap_pairs(fn):
    def w(*a, **k):
        if not STATE["cf_active"] or STATE["depth"] > 0:
            yield from fn(*a, **k)
            return
        keep = STATE["cf_keep"]
        titles = STATE["cf_titles"]
        j = 0
        for i, item in enumerate(fn(*a, **k), start=1):
            key = (STATE["cf_file"], i)
            if keep is not None and key not in keep:
                continue
            j += 1
            meta, sql = item[0], item[1]
            title = meta.get("title") or f"{j}, {sql}"
            # the title check_file will compute for this pair; duplicates are ambiguous -> no unit
            titles[title] = None if title in titles else key
            STATE["cf_cur"] = key
            yield item
            STATE["cf_cur"] = None
    w.__wrapped__ = fn
    return w


def install():
    unittest.TestCase.subTest = _subtest
    try:
        from tests.dialects import test_dialect as td
    except Exception:  # noqa: BLE001
        td = None
    if td is not None:
        V = td.Validator
        for name in ("validate_identity", "validate_all", "validate_transpile"):
            f = V.__dict__.get(name)
            if f is not None and not hasattr(f, "__wrapped__"):
                setattr(V, name, _wrap_validator(f))
    mod = sys.modules.get("tests.test_optimizer")
    if mod is not None and not hasattr(getattr(mod, "load_sql_fixture_pairs", None), "__wrapped__"):
        mod.ProcessPoolExecutor = SyncPool
        mod.load_sql_fixture_pairs = _wrap_pairs(mod.load_sql_fixture_pairs)
        T = mod.TestOptimizer
        T.check_file = _wrap_check_file(T.check_file)
