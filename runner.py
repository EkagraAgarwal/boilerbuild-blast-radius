"""Live test runner with selective-purge zygote, in-process _SyncPool (_LazyFuture),
subTest fail-fast, optmap fixture-pair slicing, packing-slack early-stop, and in-zygote TPC proxy.
"""

import atexit
import json
import os
import re
import resource
import select
import shutil
import subprocess
import sys
import time

RUN_DIR = "/tmp/br_run"
TREE = RUN_DIR + "/tree"
PYC_DIR = "/tmp/br_pyc"

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# ------------------------------------------------------------------ patch parsing

class PatchError(Exception):
    pass


def _clean_path(p):
    p = p.rstrip("\r")
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
    if p == "/dev/null":
        return None
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return _safe_rel(p)


def _safe_rel(p):
    p = os.path.normpath(p).replace("\\", "/")
    if not p or p == "." or p.startswith("/") or p == ".." or p.startswith("../"):
        raise PatchError(f"unsafe path {p!r}")
    return p


def _git_header_paths(rest):
    cands = [m.start() for m in re.finditer(r" b/", rest)]
    for c in cands:
        a, b = rest[:c], rest[c + 1:]
        if a[2:] == b[2:]:
            return a, b
    if cands:
        return rest[: cands[0]], rest[cands[0] + 1:]
    return None, None


def parse_patch(text):
    """-> list of file entries {old, new, hunks, op} (op: modify/create/delete/rename/copy)."""
    lines = text.split("\n")
    n = len(lines)
    files = []
    cur = None
    i = 0
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            a, b = _git_header_paths(line[11:].rstrip("\r"))
            cur = {"old": _clean_path(a) if a else None, "new": _clean_path(b) if b else None,
                   "hunks": [], "op": "modify", "hdr": False}
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hdr"] or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "op": "modify", "hdr": False}
                files.append(cur)
            old, new = _clean_path(line[4:]), _clean_path(lines[i + 1][4:])
            cur["hdr"] = True
            if old is None and new is None:
                raise PatchError("both sides /dev/null")
            if old is None:
                cur["op"], cur["old"], cur["new"] = "create", None, new
            elif new is None:
                cur["op"], cur["old"], cur["new"] = "delete", old, None
            else:
                cur["old"], cur["new"] = old, new
                if old != new and cur["op"] == "modify":
                    cur["op"] = "rename"
            i += 2
            continue
        if line.startswith("@@"):
            if cur is None:
                raise PatchError("hunk outside file")
            m = _HUNK.match(line)
            if not m:
                raise PatchError(f"bad hunk header {line!r}")
            o_start = int(m.group(1))
            o_len = 1 if m.group(2) is None else int(m.group(2))
            n_start = int(m.group(3))
            n_len = 1 if m.group(4) is None else int(m.group(4))
            body = []
            ol = nl = 0
            j = i + 1
            while (ol < o_len or nl < n_len) and j < n:
                l = lines[j]
                c = l[:1]
                if c == "\\":
                    if body:
                        body[-1][2] = True
                elif c == " " or l == "" or l == "\r":
                    body.append([" ", l[1:], False])
                    ol += 1
                    nl += 1
                elif c == "-":
                    body.append(["-", l[1:], False])
                    ol += 1
                elif c == "+":
                    body.append(["+", l[1:], False])
                    nl += 1
                else:
                    raise PatchError(f"bad hunk line {l[:60]!r}")
                j += 1
            if ol != o_len or nl != n_len:
                raise PatchError("truncated hunk")
            while j < n and lines[j].startswith("\\"):
                if body:
                    body[-1][2] = True
                j += 1
            cur["hunks"].append((o_start, o_len, n_start, n_len, body))
            i = j
            continue
        if cur is not None and not cur["hunks"]:
            s = line.rstrip("\r")
            if s.startswith("new file mode"):
                cur["op"] = "create"
                cur["old"] = None
            elif s.startswith("deleted file mode"):
                cur["op"] = "delete"
                cur["new"] = None
            elif s.startswith("rename from "):
                cur["op"], cur["old"] = "rename", _safe_rel(s[12:])
            elif s.startswith("rename to "):
                cur["op"], cur["new"] = "rename", _safe_rel(s[10:])
            elif s.startswith("copy from "):
                cur["op"], cur["old"] = "copy", _safe_rel(s[10:])
            elif s.startswith("copy to "):
                cur["op"], cur["new"] = "copy", _safe_rel(s[8:])
            elif s.startswith("GIT binary patch") or (s.startswith("Binary files ") and s.endswith(" differ")):
                raise PatchError("binary patch")
        i += 1
    return files


def _split(text):
    if not text:
        return [], False
    if text.endswith("\n"):
        return text[:-1].split("\n"), False
    return text.split("\n"), True


def _join(lines, noeol):
    if not lines:
        return ""
    return "\n".join(lines) + ("" if noeol else "\n")


def _apply_hunks(text, hunks, name):
    L, noeol = _split(text)
    offset = 0
    last = 0
    for o_start, o_len, n_start, n_len, body in hunks:
        old = [t for op, t, _ in body if op != "+"]
        new = [t for op, t, _ in body if op != "-"]
        want = (o_start - 1 if o_len else o_start) + offset
        pos = _find(L, old, want, last)
        if pos is None:
            pos = _find([x.rstrip("\r") for x in L], [x.rstrip("\r") for x in old], want, last)
            if pos is None:
                if not noeol and old and old[-1] == "" and len(old) >= 2:
                    old_trim = old[:-1]
                    if len(L) >= len(old_trim):
                        pos_eof = len(L) - len(old_trim)
                        if pos_eof >= last and (
                            L[pos_eof:] == old_trim
                            or [x.rstrip("\r") for x in L[pos_eof:]]
                            == [x.rstrip("\r") for x in old_trim]
                        ):
                            pos = pos_eof
                            old = old_trim
                            if (body and body[-1][0] == " " and body[-1][1] == ""
                                    and new and new[-1] == ""):
                                new = new[:-1]
                if pos is None:
                    raise PatchError(f"hunk @@ -{o_start},{o_len} does not apply to {name}")
        L[pos:pos + len(old)] = new
        last = pos + len(new)
        offset += len(new) - len(old)
        if pos + len(new) == len(L):
            if body:
                noeol = body[-1][2]
            elif not L:
                noeol = False
    return _join(L, noeol)


def _find(L, old, want, lo):
    k = len(old)
    hi = len(L) - k
    if not old:
        return min(max(want, lo), len(L))
    if hi < lo:
        return None
    want = min(max(want, lo), hi)
    first = old[0]
    for d in range(0, max(want - lo, hi - want) + 1):
        for p in (want - d, want + d) if d else (want,):
            if lo <= p <= hi and L[p] == first and L[p:p + k] == old:
                return p
    return None


def _read_src(src_root, rel):
    try:
        with open(os.path.join(src_root, rel), "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return None


def apply_patch(patch_text, src_root):
    """-> {relpath: new bytes, or None for a deleted file}. Raises PatchError."""
    if not patch_text or not patch_text.strip():
        return {}
    work = {}

    def get(rel):
        if rel in work:
            return work[rel]
        return _read_src(src_root, rel)

    for fe in parse_patch(patch_text):
        op, old, new = fe["op"], fe["old"], fe["new"]
        if op == "create":
            if new is None:
                raise PatchError("create without path")
            work[new] = _apply_hunks("", fe["hunks"], new) if fe["hunks"] else ""
        elif op == "delete":
            if old is None or get(old) is None:
                raise PatchError(f"delete of missing {old}")
            work[old] = None
        else:
            if old is None or new is None:
                raise PatchError("entry without paths")
            base = get(old)
            if base is None:
                raise PatchError(f"missing file {old}")
            out = _apply_hunks(base, fe["hunks"], old) if fe["hunks"] else base
            if op == "rename":
                work[old] = None
            work[new] = out
    return {k: (None if v is None else v.encode("utf-8", "surrogateescape")) for k, v in work.items()}


# ------------------------------------------------------------------ tree

_SRC_CACHE = {}


def _src_info(src_root):
    info = _SRC_CACHE.get(src_root)
    if info is None:
        pkg = set()
        for top in ("sqlglot", "tests"):
            for dp, dns, fns in os.walk(os.path.join(src_root, top)):
                dns[:] = [d for d in dns if d != "__pycache__"]
                if any(f.endswith(".py") for f in fns):
                    pkg.add(os.path.relpath(dp, src_root).replace("\\", "/"))
        prebuilt = os.path.isdir(os.path.join(src_root, "sqlglot", "__pycache__"))
        info = _SRC_CACHE[src_root] = {"pkg": pkg, "prebuilt": prebuilt, "ls": {}}
    return info


def _listdir(info, path):
    ls = info["ls"].get(path)
    if ls is None:
        try:
            ls = [e for e in os.listdir(path) if e != "__pycache__"]
        except (FileNotFoundError, NotADirectoryError):
            ls = []
        info["ls"][path] = ls
    return ls


def _rmtree(p):
    if os.path.lexists(p):
        shutil.rmtree(p, ignore_errors=True)


def build_tree(src_root, changes, tree=TREE):
    info = _src_info(src_root)
    prebuilt, pkg = info["prebuilt"], info["pkg"]
    kids = {}
    for rel in changes:
        parts = rel.split("/")
        for k in range(len(parts)):
            kids.setdefault("/".join(parts[:k]), set()).add(parts[k])
    mat = set(kids)
    if not prebuilt:
        mat |= pkg
    mat.add("")
    for d in list(mat):
        while d:
            d = d.rpartition("/")[0]
            mat.add(d)
    changed_dirs = {rel.rpartition("/")[0] for rel in changes}

    def build(rel):
        sdir = os.path.join(src_root, rel) if rel else src_root
        tdir = os.path.join(tree, rel) if rel else tree
        os.mkdir(tdir)
        names = set(_listdir(info, sdir)) | kids.get(rel, set())
        has_py = False
        for name in names:
            r = f"{rel}/{name}" if rel else name
            if name.endswith(".py"):
                has_py = True
            if r in changes:
                data = changes[r]
                if data is not None:
                    with open(os.path.join(tdir, name), "wb") as f:
                        f.write(data)
            elif r in mat:
                build(r)
            else:
                os.symlink(os.path.join(sdir, name), os.path.join(tdir, name))
        if not rel or not (has_py or rel in pkg):
            return
        spc = os.path.join(sdir, "__pycache__")
        store = spc if os.path.isdir(spc) else os.path.join(PYC_DIR, rel, "__pycache__")
        if store != spc:
            os.makedirs(store, exist_ok=True)
        tpc = os.path.join(tdir, "__pycache__")
        if rel not in changed_dirs:
            os.symlink(store, tpc)
            return
        os.mkdir(tpc)
        stems = {r.rpartition("/")[2][:-3] for r in changes if r.endswith(".py") and r.rpartition("/")[0] == rel}
        for f in os.listdir(store):
            if f.split(".", 1)[0] not in stems:
                os.symlink(os.path.join(store, f), os.path.join(tpc, f))

    _rmtree(tree)
    os.makedirs(os.path.dirname(tree), exist_ok=True)
    build("")
    return tree


# ------------------------------------------------------------------ zygote (runs in a child python)

_ZYGOTE = r'''
import concurrent.futures, concurrent.futures.process, gc, json, os, pickle, resource, signal, sys
tree, preload = sys.argv[1], sys.argv[2]
lite = len(sys.argv) > 3 and sys.argv[3] == "1"
sys.path[:] = [tree] + [p for p in sys.path if p not in ("", ".", tree)]
os.chdir(tree)

def _try(m):
    try:
        __import__(m)
    except BaseException:
        pass

if lite:
    for m in ("unittest", "unittest.mock", "json", "tempfile", "decimal", "datetime", "importlib", "sqlglot"):
        _try(m)
else:
    for m in ("unittest", "unittest.mock", "json", "sqlite3", "tempfile", "decimal", "datetime",
              "importlib", "concurrent.futures", "concurrent.futures.process", "multiprocessing",
              "multiprocessing.context", "multiprocessing.popen_fork", "numpy", "pandas",
              "pandas.testing", "duckdb"):
        _try(m)
    if preload == "1":
        for m in ("sqlglot", "sqlglot.optimizer", "sqlglot.executor", "sqlglot.planner"):
            _try(m)
os.chdir("/")
import importlib, tempfile, unittest
gc.collect()
gc.freeze()


def send(o):
    os.write(1, (json.dumps(o) + "\n").encode())


def cpu(ru):
    return ru.ru_utime + ru.ru_stime


class _TestFailedFast(BaseException):
    pass


_CHECK_PICKLE = False


class _LazyFuture(concurrent.futures.Future):
    def __init__(self, fn, args, kwargs):
        super().__init__()
        self._task = (fn, args, kwargs)
        self._ran = False
        self._val = None
        self._exc = None
        self.set_result(None)

    def _exec(self):
        if not self._ran:
            self._ran = True
            fn, args, kwargs = self._task
            self._task = None
            try:
                if _CHECK_PICKLE:
                    fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                val = fn(*args, **kwargs)
                if _CHECK_PICKLE:
                    val = pickle.loads(pickle.dumps(val))
                self._val = val
            except BaseException as exc:
                self._exc = exc

    def result(self, timeout=None):
        self._exec()
        if self._exc is not None:
            raise self._exc
        return self._val

    def exception(self, timeout=None):
        self._exec()
        return self._exc


class _SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        return _LazyFuture(fn, args, dict(kwargs))

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futs = [self.submit(fn, *args) for args in zip(*iterables)]
        return (f.result() for f in futs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()


def _pack_cost(durs):
    if not durs:
        return 0
    order = sorted(durs, reverse=True)
    best = 10**12
    for k in range(1, 9):
        loads = [0] * k
        for d in order:
            i = loads.index(min(loads))
            loads[i] += d
        c = max(loads) + 2000 * k
        if c < best:
            best = c
    return best


def _run_tpcds_proxy(req):
    from sqlglot import exp, parse_one
    from sqlglot.optimizer import optimizer
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs, string_to_bool

    suite = req.get("suite") or "tpcds"
    subset_str = req.get("subset") or ""
    only = {int(x) for x in subset_str.split(",") if x} if subset_str else None
    if suite == "tpch":
        base_schema = TPCH_SCHEMA
        pairs = list(enumerate(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"), start=1))
    else:
        base_schema = TPCDS_SCHEMA
        pairs = list(enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"), start=1))

    cases = [
        (i, meta, sql, expected)
        for i, (meta, sql, expected) in pairs
        if only is None or (i - 1) in only
    ]
    cases.sort(key=lambda c: (len(c[2]), c[0]))

    for i, meta, sql, expected in cases:
        dialect = meta.get("dialect")
        kwargs = {"schema": base_schema}
        if schema_override := meta.get("schema"):
            kwargs["schema"] = json.loads(schema_override)
        for key in ("leave_tables_isolated", "validate_qualify_columns", "canonicalize_table_aliases"):
            if meta.get(key) is not None:
                kwargs[key] = string_to_bool(meta.get(key))
        if dialect:
            kwargs["dialect"] = dialect
        try:
            optimized = optimizer.optimize(parse_one(sql, read=dialect), **kwargs)
            for expression in optimized.walk():
                for arg_key, arg in expression.args.items():
                    for child_node in arg if isinstance(arg, list) else [arg]:
                        if isinstance(child_node, exp.Expr):
                            assert child_node.arg_key == arg_key
                            assert child_node.parent is expression
            optimized = pickle.loads(pickle.dumps(optimized))
            actual = optimized.sql(pretty=True, dialect=dialect)
        except BaseException as e:
            return {"ok": False, "why": f"query {i}: {type(e).__name__}: {e}"[:80]}
        if actual != expected:
            return {"ok": False, "why": f"query {i}: mismatch"}
    return {"ok": True, "why": "passed"}


def run_ids(ids, ms_map=None, fixed_ms=None, early_stop=False, opt_slices=None, fail_fast=True):
    import unittest.case, unittest.suite

    if opt_slices:
        try:
            import tests.helpers as _th
            _orig_lfp = _th.load_sql_fixture_pairs
            _slice_sets = {k: set(v) for k, v in opt_slices.items()}
            def _sliced_lfp(path, *a, **kw):
                rows = list(_orig_lfp(path, *a, **kw))
                allow = _slice_sets.get(path)
                if allow is None:
                    return rows
                return [r for idx, r in enumerate(rows, 1) if idx in allow]
            _th.load_sql_fixture_pairs = _sliced_lfp
        except BaseException:
            pass

    groups = {}
    failed = set()
    remaining = set()
    for tid in ids:
        f, sep, rest = tid.partition("::")
        parts = rest.split("::")
        if not sep or not f.endswith(".py") or len(parts) != 2:
            failed.add(tid)
            continue
        groups.setdefault(f[:-3].replace("/", "."), {}).setdefault(parts[0], []).append((parts[1], tid))
        remaining.add(tid)

    ms_lookup = ms_map or {}
    fixed_list = list(fixed_ms or ())

    ordered_mods = []
    for modname, classes in groups.items():
        ord_cls = []
        mmax = 0
        for cname, meths in classes.items():
            meths.sort(key=lambda x: (-ms_lookup.get(x[1], 0), x[1]))
            cmax = max((ms_lookup.get(t, 0) for _, t in meths), default=0)
            if cmax > mmax:
                mmax = cmax
            ord_cls.append((cname, meths, cmax))
        ord_cls.sort(key=lambda x: (-x[2], x[0]))
        ordered_mods.append((modname, ord_cls, mmax))
    ordered_mods.sort(key=lambda x: (-x[2], x[0]))

    def check_stop():
        if not early_stop or not ms_lookup:
            return False
        if not remaining:
            return True
        base_durs = fixed_list + [ms_lookup.get(t, 0) for t in failed]
        lower_ms = _pack_cost(base_durs)
        upper_ms = _pack_cost(base_durs + [ms_lookup.get(t, 0) for t in remaining])
        return (upper_ms - lower_ms) <= 25

    in_call = [False]

    for modname, ord_cls, _mmax in ordered_mods:
        if check_stop():
            break
        try:
            mod = importlib.import_module(modname)
        except BaseException:
            for _cname, meths, _cmax in ord_cls:
                for _, t in meths:
                    failed.add(t)
                    remaining.discard(t)
            continue
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = _SyncPool

        suite = unittest.TestSuite()
        owner = {}
        by_cls = {}
        by_mod = {}
        for cname, meths, _cmax in ord_cls:
            cls = getattr(mod, cname, None)
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                for _, t in meths:
                    failed.add(t)
                    remaining.discard(t)
                continue
            key = f"{cls.__module__}.{cls.__qualname__}"
            for meth, tid in meths:
                if not callable(getattr(cls, meth, None)):
                    failed.add(tid)
                    remaining.discard(tid)
                    continue
                try:
                    inst = cls(meth)
                except BaseException:
                    failed.add(tid)
                    remaining.discard(tid)
                    continue
                if fail_fast:
                    orig_call = inst._callTestMethod
                    def _wrapped_call(method, _orig=orig_call):
                        in_call[0] = True
                        try:
                            _orig(method)
                        except _TestFailedFast:
                            pass
                        finally:
                            in_call[0] = False
                    inst._callTestMethod = _wrapped_call
                owner[id(inst)] = tid
                by_cls.setdefault(key, []).append(tid)
                by_mod.setdefault(cls.__module__, []).append(tid)
                suite.addTest(inst)

        class R(unittest.TestResult):
            def _mark(self, test):
                tid = owner.get(id(test))
                if tid is not None:
                    failed.add(tid)
                    remaining.discard(tid)
                    if check_stop():
                        self.shouldStop = True
                    return
                desc = getattr(test, "description", "") or str(test)
                name = desc.partition(" (")[2].rstrip(")")
                hit = by_cls.get(name) or by_mod.get(name)
                if hit is None:
                    hit = [t for k, v in by_cls.items() if name.startswith(k) or k.startswith(name) for t in v] or list(owner.values())
                failed.update(hit)
                remaining.difference_update(hit)
                if check_stop():
                    self.shouldStop = True

            def addError(self, test, err):
                self._mark(test)

            def addFailure(self, test, err):
                self._mark(test)

            def addSubTest(self, test, subtest, err):
                if err is not None:
                    if err[0] is not None and issubclass(err[0], unittest.SkipTest):
                        return
                    self._mark(test)
                    if fail_fast and in_call[0]:
                        raise _TestFailedFast()

            def addUnexpectedSuccess(self, test):
                self._mark(test)

            def addSkip(self, test, reason):
                pass

            def addExpectedFailure(self, test, err):
                pass

            def stopTest(self, test):
                tid = owner.get(id(test))
                if tid is not None:
                    remaining.discard(tid)
                if check_stop():
                    self.shouldStop = True

        if owner:
            suite(R())

    return sorted(failed | remaining)


def child(req):
    os.setsid()
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(int(req["timeout"]) + 5)
    fd = os.open(os.devnull, os.O_RDWR)
    for k in (0, 1, 2):
        os.dup2(fd, k)
    os.makedirs(req["tmp"], exist_ok=True)
    os.environ["TMPDIR"] = req["tmp"]
    tempfile.tempdir = None
    os.environ.pop("PYTHONPATH", None)
    os.chdir(tree)
    sys.dont_write_bytecode = bool(req["dwb"])
    cmods = req.get("changed_mods")
    if not req["purge"]:
        need_purge = False
    elif cmods is not None and "__NON_PY__" not in cmods:
        need_purge = any(m in sys.modules for m in cmods)
    else:
        need_purge = True
    purge = ("tests",) + (("sqlglot",) if need_purge else ())
    for name in list(sys.modules):
        if name.split(".", 1)[0] in purge:
            del sys.modules[name]
    importlib.invalidate_caches()
    op = req.get("op")
    if op == "import_check":
        res = {"ok": False, "why": "", "ok_tests": []}
        try:
            import sqlglot
            if not os.path.abspath(sqlglot.__file__).startswith(tree + os.sep):
                raise RuntimeError("sqlglot imported from outside the run tree")
            for m in req["sql_mods"]:
                importlib.import_module(m)
            ok_t = []
            for fl, m in req["test_mods"]:
                try:
                    importlib.import_module(m)
                    ok_t.append(fl)
                except BaseException:
                    pass
            res = {"ok": True, "why": "", "ok_tests": ok_t}
        except BaseException as e:
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:80], "ok_tests": []}
        tmp = req["out"] + ".part"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.replace(tmp, req["out"])
        return 0
    if op == "tpcds_proxy":
        try:
            import sqlglot
            if not os.path.abspath(sqlglot.__file__).startswith(tree + os.sep):
                raise RuntimeError("sqlglot imported from outside the run tree")
            res = _run_tpcds_proxy(req)
        except BaseException as e:
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:80]}
        tmp = req["out"] + ".part"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.replace(tmp, req["out"])
        return 0
    if req.get("sync_pool", True):
        global _CHECK_PICKLE
        cfiles = set(req.get("changed_files") or ())
        _CHECK_PICKLE = bool({"sqlglot/serde.py", "sqlglot/expressions/core.py"} & cfiles)
        concurrent.futures.ProcessPoolExecutor = _SyncPool
        concurrent.futures.process.ProcessPoolExecutor = _SyncPool
    elif req["fork_pool"]:
        import multiprocessing
        orig = multiprocessing.get_context
        def get_context(method=None):
            if method in ("spawn", "forkserver"):
                method = "fork"
            return orig(method)
        multiprocessing.get_context = get_context
    try:
        import sqlglot
    except BaseException:
        sqlglot = None
    if sqlglot is not None and not os.path.abspath(sqlglot.__file__).startswith(tree + os.sep):
        raise RuntimeError("sqlglot imported from outside the run tree")
    failed = run_ids(
        req["ids"],
        ms_map=req.get("ms_map"),
        fixed_ms=req.get("fixed_ms"),
        early_stop=bool(req.get("early_stop", False)),
        opt_slices=req.get("opt_slices"),
        fail_fast=bool(req.get("fail_fast", True)),
    )
    tmp = req["out"] + ".part"
    with open(tmp, "w") as f:
        json.dump({"failed": failed}, f)
    os.replace(tmp, req["out"])
    return 0


send({"ready": True, "cpu": cpu(resource.getrusage(resource.RUSAGE_SELF))})
inp = sys.stdin.buffer
while True:
    line = inp.readline()
    if not line:
        break
    req = json.loads(line)
    z0 = cpu(resource.getrusage(resource.RUSAGE_SELF))
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            code = child(req)
        except BaseException:
            code = 3
        os._exit(code)
    send({"pid": pid})
    while True:
        try:
            _, status, ru = os.wait4(pid, 0)
            break
        except ChildProcessError:
            status, ru = -1, None
            break
        except InterruptedError:
            continue
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    z1 = cpu(resource.getrusage(resource.RUSAGE_SELF))
    send({"done": pid, "status": status, "cpu": cpu(ru) if ru else 0.0, "zcpu": z1 - z0})
'''


class _Zygote:
    def __init__(self, src_root, preload, dwb, lite=False):
        self.src_root, self.preload, self.dwb, self.lite = src_root, preload, dwb, lite
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["SKIP_INTEGRATION"] = "0"
        env["PYTHON_CPU_COUNT"] = "1"
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            env.setdefault(k, "1")
        if dwb:
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        else:
            env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.setdefault("HOME", "/tmp")
        self.buf = b""
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _ZYGOTE, TREE, "1" if preload else "0", "1" if lite else "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd="/", bufsize=0, close_fds=True,
        )
        msg = self.read(120.0)
        if not msg or not msg.get("ready"):
            self.close()
            raise RuntimeError("zygote failed to start")
        self.startup_cpu = msg["cpu"]

    def read(self, timeout):
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError("zygote exited")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line)

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def close(self):
        p = self.proc
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(5)
        except Exception:
            p.kill()
            try:
                p.wait(5)
            except Exception:
                pass


_Z = None
_Z_LITE = None
_STATS = {"calls": 0, "zygote_starts": 0, "startup_cpu_s": 0.0}


def _zygote(src_root, lite=False):
    global _Z, _Z_LITE
    if lite:
        if _Z_LITE is not None and (_Z_LITE.src_root != src_root or _Z_LITE.proc.poll() is not None):
            _Z_LITE.close()
            _Z_LITE = None
        if _Z_LITE is None:
            build_tree(src_root, {})
            info = _src_info(src_root)
            _Z_LITE = _Zygote(src_root, preload=False, dwb=info["prebuilt"], lite=True)
            _STATS["zygote_starts"] += 1
            _STATS["startup_cpu_s"] += _Z_LITE.startup_cpu
            return _Z_LITE, _Z_LITE.startup_cpu
        return _Z_LITE, 0.0
    info = _src_info(src_root)
    if _Z is not None and (_Z.src_root != src_root or _Z.proc.poll() is not None):
        _Z.close()
        _Z = None
    if _Z is None:
        build_tree(src_root, {})
        _Z = _Zygote(src_root, preload=True, dwb=info["prebuilt"])
        _STATS["zygote_starts"] += 1
        _STATS["startup_cpu_s"] += _Z.startup_cpu
        return _Z, _Z.startup_cpu
    return _Z, 0.0


def _cpu_self():
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def _cpu_children():
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _kill_group(pid):
    for fn in (os.killpg, os.kill):
        try:
            fn(pid, signal.SIGKILL)
        except OSError:
            pass


def _changed_sqlglot_mods(changes):
    mods = []
    for r in changes:
        if not r.startswith("sqlglot/"):
            continue
        if not r.endswith(".py"):
            mods.append("__NON_PY__")
            continue
        m = r[:-3]
        if m.endswith("/__init__"):
            m = m[:-len("/__init__")]
        mods.append(m.replace("/", "."))
    return mods


def check_imports(patch_text, sql_mods, test_mods, src_root="/sqlglot", timeout_s=30.0):
    """Import sql_mods (and optional [(file, mod), ...] test_mods) on the patched tree via _Z_LITE."""
    global _Z_LITE
    try:
        changes = apply_patch(patch_text, src_root)
    except (PatchError, ValueError, UnicodeError) as e:
        return {"ok": False, "why": f"patch: {e}"[:80], "ok_tests": []}
    out = os.path.join(RUN_DIR, "imp_result.json")
    pid = None
    try:
        z, _startup = _zygote(src_root, lite=True)
        build_tree(src_root, changes)
        _rmtree(out)
        z.send({
            "op": "import_check",
            "sql_mods": list(sql_mods),
            "test_mods": list(test_mods),
            "out": out,
            "tmp": os.path.join(RUN_DIR, "tmp"),
            "timeout": timeout_s,
            "purge": any(r.split("/", 1)[0] == "sqlglot" for r in changes),
            "changed_mods": _changed_sqlglot_mods(changes),
            "dwb": z.dwb,
            "fork_pool": False,
        })
        deadline = time.monotonic() + timeout_s
        msg = z.read(30.0)
        if not msg or "pid" not in msg:
            raise RuntimeError("zygote did not fork")
        pid = msg["pid"]
        msg = z.read(max(0.0, deadline - time.monotonic()))
        if msg is None:
            _kill_group(pid)
            z.read(30.0)
            return {"ok": False, "why": f"timeout after {timeout_s}s", "ok_tests": []}
        with open(out) as f:
            return json.load(f)
    except Exception as e:
        if pid:
            _kill_group(pid)
        if _Z_LITE is not None:
            _Z_LITE.close()
            _Z_LITE = None
        return {"ok": False, "why": f"error {e!r}"[:80], "ok_tests": []}
    finally:
        _rmtree(RUN_DIR)


def run_tpcds_proxy(patch_text, subset="", suite="tpcds", src_root="/sqlglot", timeout_s=40.0):
    """Replay TPC-DS / TPC-H optimizer queries inside forked _Z_LITE child."""
    global _Z_LITE
    s0, c0 = _cpu_self(), _cpu_children()
    try:
        changes = apply_patch(patch_text, src_root)
    except (PatchError, ValueError, UnicodeError) as e:
        return {"ok": False, "why": f"patch: {e}"[:80], "cpu_s": 0.0}
    out = os.path.join(RUN_DIR, "proxy_result.json")
    pid = None
    try:
        z, startup = _zygote(src_root, lite=True)
        build_tree(src_root, changes)
        _rmtree(out)
        z.send({
            "op": "tpcds_proxy",
            "subset": subset,
            "suite": suite,
            "out": out,
            "tmp": os.path.join(RUN_DIR, "tmp"),
            "timeout": timeout_s,
            "purge": any(r.split("/", 1)[0] == "sqlglot" for r in changes),
            "changed_mods": _changed_sqlglot_mods(changes),
            "dwb": z.dwb,
            "fork_pool": False,
        })
        deadline = time.monotonic() + timeout_s
        msg = z.read(30.0)
        if not msg or "pid" not in msg:
            raise RuntimeError("zygote did not fork")
        pid = msg["pid"]
        msg = z.read(max(0.0, deadline - time.monotonic()))
        if msg is None:
            _kill_group(pid)
            m2 = z.read(30.0)
            cpu = (m2["cpu"] + m2["zcpu"]) if m2 else 0.0
            return {"ok": False, "why": f"timeout after {timeout_s}s", "cpu_s": cpu + startup}
        cpu = msg["cpu"] + msg["zcpu"] + startup + (_cpu_self() - s0) + (_cpu_children() - c0)
        with open(out) as f:
            res = json.load(f)
        res["cpu_s"] = cpu
        return res
    except Exception as e:
        if pid:
            _kill_group(pid)
        if _Z_LITE is not None:
            _Z_LITE.close()
            _Z_LITE = None
        return {"ok": False, "why": f"error {e!r}"[:80], "cpu_s": 0.0}
    finally:
        _rmtree(RUN_DIR)


def run_tests(
    patch_text,
    test_ids,
    src_root="/sqlglot",
    timeout_s=20.0,
    _fork_pool=True,
    lite=False,
    ms_map=None,
    fixed_ms=None,
    early_stop=False,
    opt_slices=None,
    fail_fast=True,
):
    global _Z, _Z_LITE
    t_wall = time.monotonic()
    s0, c0 = _cpu_self(), _cpu_children()
    ids = list(dict.fromkeys(test_ids))
    detail = {"child": 0.0, "zygote": 0.0, "startup": 0.0, "parent": 0.0, "wall": 0.0}
    _STATS["calls"] += 1

    def finish(failed, ok, error):
        detail["parent"] = (_cpu_self() - s0) + (_cpu_children() - c0)
        detail["wall"] = time.monotonic() - t_wall
        cpu = detail["child"] + detail["zygote"] + detail["startup"] + detail["parent"]
        _rmtree(RUN_DIR)
        return {"failed": set(failed), "ok": ok, "cpu_s": cpu, "error": error, "detail": detail}

    try:
        changes = apply_patch(patch_text, src_root)
    except (PatchError, ValueError, UnicodeError) as e:
        return finish(ids, False, f"patch: {e}")
    if not ids:
        return finish([], True, None)
    out = os.path.join(RUN_DIR, "result.json")
    pid = None
    try:
        z, startup = _zygote(src_root, lite=lite)
        detail["startup"] = startup
        build_tree(src_root, changes)
        _rmtree(out)
        z.send({
            "ids": ids,
            "out": out,
            "tmp": os.path.join(RUN_DIR, "tmp"),
            "timeout": timeout_s,
            "purge": any(r.split("/", 1)[0] == "sqlglot" for r in changes),
            "changed_mods": _changed_sqlglot_mods(changes),
            "changed_files": list(changes),
            "dwb": z.dwb,
            "fork_pool": bool(_fork_pool),
            "sync_pool": True,
            "ms_map": ms_map,
            "fixed_ms": fixed_ms,
            "early_stop": bool(early_stop),
            "opt_slices": opt_slices,
            "fail_fast": bool(fail_fast),
        })
        deadline = time.monotonic() + timeout_s
        msg = z.read(30.0)
        if not msg or "pid" not in msg:
            raise RuntimeError("zygote did not fork")
        pid = msg["pid"]
        msg = z.read(max(0.0, deadline - time.monotonic()))
        timed_out = msg is None
        if timed_out:
            _kill_group(pid)
            msg = z.read(30.0)
            if msg is None:
                raise RuntimeError("zygote stuck after kill")
        detail["child"], detail["zygote"] = msg["cpu"], msg["zcpu"]
        if timed_out:
            return finish(ids, False, f"timeout after {timeout_s}s")
        try:
            with open(out) as f:
                failed = json.load(f)["failed"]
        except (OSError, ValueError, KeyError):
            return finish(ids, False, f"child produced no result (wait status {msg.get('status')})")
        return finish(failed, True, None)
    except Exception as e:
        if pid:
            _kill_group(pid)
        if lite:
            if _Z_LITE is not None:
                _Z_LITE.close()
                _Z_LITE = None
        else:
            if _Z is not None:
                _Z.close()
                _Z = None
        return finish(ids, False, f"runner: {type(e).__name__}: {e}")


def stats():
    return dict(_STATS)


def shutdown(clear_cache=True):
    global _Z, _Z_LITE
    if _Z is not None:
        _Z.close()
        _Z = None
    if _Z_LITE is not None:
        _Z_LITE.close()
        _Z_LITE = None
    _rmtree(RUN_DIR)
    if clear_cache:
        _rmtree(PYC_DIR)


atexit.register(shutdown)
