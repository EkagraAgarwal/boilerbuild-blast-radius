"""Verify-by-running: apply a patch to a cheap mirror of sqlglot in /tmp and run tests.

    run_tests(patch_text, test_ids, src_root="/sqlglot", timeout_s=20.0) -> dict
        {"failed": set(ids), "ok": bool, "cpu_s": float, "error": str|None, "detail": {...}}

Design
- The patched tree lives at /tmp/br_run/tree. Unchanged files/dirs are symlinks into the
  read-only pristine checkout; only patched files are real copies. Every Python package
  dir is a real dir of symlinks whose __pycache__ points at a persistent bytecode store
  (/tmp/br_pyc) that only ever receives bytecode compiled from pristine sources, so
  repeated runs skip compiling sqlglot. Dirs with a patched .py get a private
  __pycache__ (deleted with the tree), so patched bytecode is never cached or reused.
  If src_root already ships __pycache__ (compileall at build time), that is used instead.
- A lazily started fork server ("zygote") pre-imports the heavy third-party modules the
  tests use (unittest.mock, numpy, pandas, duckdb) and pristine sqlglot. Each run is a
  fork of it. If the patch touches sqlglot/, the child drops sqlglot from sys.modules and
  imports the patched copy. tests.* are never preloaded.
- The child runs the requested ids with the stdlib unittest machinery (one suite, so
  setUpClass/setUpModule run as usual). Failure, error, unexpected success, a failing
  subTest, an error in a class/module fixture, a module import error or a missing test
  all count as failed; skips do not.
- The tests' spawn ProcessPoolExecutors (TestOptimizer.setUpClass, executor tpch/tpcds)
  would re-import pandas/duckdb/sqlglot in up to os.cpu_count() fresh interpreters.
  The child maps multiprocessing.get_context("spawn") to "fork" and PYTHON_CPU_COUNT=1
  keeps it to one worker, so arguments and results are still pickled across a real
  process boundary but without the interpreter start-ups.
- cpu_s = forked child (os.wait4 rusage, includes its pool workers) + zygote bookkeeping +
  this process's own work, plus the zygote's start-up cost on the call that started it.
"""

import atexit
import json
import os
import re
import resource
import select
import shutil
import signal
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
    # "a/X b/Y": try the split where both halves name the same path first.
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
            body = []  # [op, text, noeol]
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
    last = 0  # hunks must apply in order, after the previous one
    for o_start, o_len, n_start, n_len, body in hunks:
        old = [t for op, t, _ in body if op != "+"]
        new = [t for op, t, _ in body if op != "-"]
        want = (o_start - 1 if o_len else o_start) + offset
        pos = _find(L, old, want, last)
        if pos is None:
            pos = _find([x.rstrip("\r") for x in L], [x.rstrip("\r") for x in old], want, last)
            if pos is None:
                # A hunk that edits/appends at EOF can carry a synthetic trailing
                # blank context line (" " -> "") even though _split stripped the
                # file's final "\n", making len(old) exceed len(L) - pos by one.
                # Retry the match at EOF with that trailing "" dropped.
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
                            # Only drop the synthetic EOF "new" blank line when the
                            # hunk's last body line is that empty context line. When
                            # it is a "+" (e.g. appending a method), keep new as-is
                            # so the blank separator before the new code survives.
                            if (body and body[-1][0] == " " and body[-1][1] == ""
                                    and new and new[-1] == ""):
                                new = new[:-1]
                if pos is None:
                    raise PatchError(f"hunk @@ -{o_start},{o_len} does not apply to {name}")
        at_end = pos + len(old) == len(L)
        L[pos:pos + len(old)] = new
        last = pos + len(new)
        offset = pos - (o_start - 1 if o_len else o_start) + (len(new) - len(old))
        if at_end and body:
            news = [b for b in body if b[0] != "-"]
            if news:
                noeol = news[-1][2]
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
    work = {}  # rel -> str | None

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
    mat = set(kids)  # every ancestor dir of a changed path
    if not prebuilt:
        mat |= pkg
    mat.add("")
    for d in list(mat):  # ancestors of materialized dirs
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
import gc, json, os, resource, signal, sys
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


def run_ids(ids):
    import unittest.case, unittest.suite
    groups = {}
    failed = set()
    for tid in ids:
        f, sep, rest = tid.partition("::")
        parts = rest.split("::")
        if not sep or not f.endswith(".py") or len(parts) != 2:
            failed.add(tid)
            continue
        groups.setdefault(f[:-3].replace("/", "."), {}).setdefault(parts[0], []).append((parts[1], tid))
    suite = unittest.TestSuite()
    owner = {}   # id(test instance) -> pytest id
    insts = []
    by_cls = {}  # "module.Class" -> [pytest ids]
    by_mod = {}
    for modname, classes in groups.items():
        try:
            mod = importlib.import_module(modname)
        except BaseException:
            for meths in classes.values():
                failed.update(t for _, t in meths)
            continue
        for cname, meths in classes.items():
            cls = getattr(mod, cname, None)
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                failed.update(t for _, t in meths)
                continue
            key = f"{cls.__module__}.{cls.__qualname__}"
            for meth, tid in meths:
                if not callable(getattr(cls, meth, None)):
                    failed.add(tid)
                    continue
                try:
                    inst = cls(meth)
                except BaseException:
                    failed.add(tid)
                    continue
                insts.append(inst)
                owner[id(inst)] = tid
                by_cls.setdefault(key, []).append(tid)
                by_mod.setdefault(cls.__module__, []).append(tid)
                suite.addTest(inst)

    class R(unittest.TestResult):
        def _mark(self, test):
            tid = owner.get(id(test))
            if tid is not None:
                failed.add(tid)
                return
            desc = getattr(test, "description", "") or str(test)
            name = desc.partition(" (")[2].rstrip(")")
            hit = by_cls.get(name) or by_mod.get(name)
            if hit is None:
                hit = [t for k, v in by_cls.items() if name.startswith(k) or k.startswith(name) for t in v] or list(owner.values())
            failed.update(hit)

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

        def addSkip(self, test, reason):
            pass

        def addExpectedFailure(self, test, err):
            pass

    suite(R())
    return sorted(failed)


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
    if req.get("op") == "import_check":
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
    if req["fork_pool"]:
        import multiprocessing
        orig = multiprocessing.get_context
        def get_context(method=None):
            if method in ("spawn", "forkserver"):
                method = "fork"
            return orig(method)
        multiprocessing.get_context = get_context
    try:
        import sqlglot  # a patch that breaks this import fails every test via module import
    except BaseException:
        sqlglot = None
    if sqlglot is not None and not os.path.abspath(sqlglot.__file__).startswith(tree + os.sep):
        raise RuntimeError("sqlglot imported from outside the run tree")
    failed = run_ids(req["ids"])
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
            build_tree(src_root, {})  # pristine tree so preloaded sqlglot.__path__ points inside TREE
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
        build_tree(src_root, {})  # pristine tree for the preload
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


def run_tests(patch_text, test_ids, src_root="/sqlglot", timeout_s=20.0, _fork_pool=True, lite=False):
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
        z.send({"ids": ids, "out": out, "tmp": os.path.join(RUN_DIR, "tmp"), "timeout": timeout_s,
                "purge": any(r.split("/", 1)[0] == "sqlglot" for r in changes),
                "changed_mods": _changed_sqlglot_mods(changes),
                "dwb": z.dwb, "fork_pool": bool(_fork_pool)})
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
    except Exception as e:  # zygote broken: restart on next call
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
