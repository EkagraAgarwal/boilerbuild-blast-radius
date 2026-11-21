"""Partial verification of TestExecutor's TPC tests: run only the fixture queries a patch touches.

    analyze(patch_text, querymap, which, src_root) -> {"indices", "touches_setup", "reason", "est_cpu"}
    queries_touching(querymap, which, changed)     -> (indices, touches_setup)
    verify(patch_text, which, query_indices, src_root="/sqlglot", timeout_s=60)
        -> {"ok": bool, "failed": bool, "cpu_s": float, "error": str|None, ...}

which: "tpch"     tests/test_executor.py::TestExecutor::test_execute_tpch
       "tpcds"    tests/test_executor.py::TestExecutor::test_execute_tpcds
       "tpch_opt" tests/test_executor.py::TestExecutor::test_optimized_tpch

querymap.json.gz (tools/brquery.py + tools/export_querymap.py) maps each (file, line) of pristine
sqlglot to the query indices whose run executes it, plus the lines run by shared setup (imports,
setUpClass, initializer pickling). If no changed line is in setup and the touched queries pass
against the patched code, the whole test passes. touches_setup=True means "select the real
test": verify(..., range(n)) does NOT stand in for it (setUpClass is not reproduced: tpch builds
no Table, tpcds only the tables executed queries read), and it costs more than the test anyway.

verify() reproduces, for the given indices only and in a forked child running the PATCHED
tree, what the test does: parse_one each query; pickle (mp_execute, (expr, meta)) across the
"process boundary"; the test module's own initializer/mp_execute; pickle the result back;
TestExecutor.subtestHelper (duckdb on transpile(sql, write="duckdb"), DataFrame,
assert_frame_equal). Tables are built (setUpClass's CSV->Table logic) only when mp_execute asks
for them; the CSV rows (no sqlglot involved) are parsed once in the server and reused across
calls; the initargs pickle round trip of tables is skipped (Table has no pickle hooks). duckdb
results are computed once in the server from the PRISTINE duckdb SQL (querymap "duck") and reused
when the patched transpile emits the same text; otherwise the child runs duckdb itself (views
only for tables the query references). The server fills these caches for all requested indices
before forking, so a cold call does not fail fast. Any exception or mismatch =>
failed=True. ok=False means the check itself could not run (patch did not apply, harness files
patched, timeout, crash): treat as "run the real test".

The child is a fork of a lazily started server that has numpy/pandas/duckdb imported; the
patched tree is runner.build_tree's symlink mirror at /tmp/br_part/tree. cpu_s = child rusage
(wait4) + server bookkeeping + this process + server start-up on the call that started it.
"""

import base64
import gzip
import json
import os
import resource
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

PART_DIR = "/tmp/br_part"
TREE = PART_DIR + "/tree"
QUERYMAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "querymap.json.gz")

TEST_IDS = {
    "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tpch_opt": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}
# files the reproduction (and the query indices) depend on besides sqlglot/
_HARNESS_FILES = {"tests/test_executor.py", "tests/helpers.py", "tests/__init__.py", "tests/fixtures/__init__.py"}
_HARNESS_DIRS = ("tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")


def _is_harness(rel):
    return rel in _HARNESS_FILES or rel.startswith(_HARNESS_DIRS)


# ------------------------------------------------------------------ map lookup

_QM = None


def load_querymap(path=QUERYMAP):
    global _QM
    if _QM is None or _QM[0] != path:
        with gzip.open(path, "rt") as f:
            _QM = (path, json.load(f))
    return _QM[1]


_SETUP_CACHE = {}


def queries_touching(querymap, which, changed):
    """changed: {relpath: set(old-side line numbers)} -> (sorted query indices, touches_setup)."""
    sec = querymap[which]
    n = sec["n"]
    setup = querymap.get("setup_lines", {})
    sc = _SETUP_CACHE.get(id(querymap))
    if sc is None or sc[0] is not querymap:
        sc = _SETUP_CACHE[id(querymap)] = (querymap, {})
    acc = 0
    touches = False
    for rel, lines in changed.items():
        fl = sec["lines"].get(rel)
        if rel not in sc[1]:
            sc[1][rel] = set(setup.get(rel, ()))
        sl = sc[1][rel]
        for ln in lines:
            if ln in sl:
                touches = True
            if fl:
                v = fl.get(str(ln))
                if v:
                    acc |= int.from_bytes(base64.b64decode(v), "little")
    return [i for i in range(n) if (acc >> i) & 1], touches


def _code_line(text):
    s = text.strip()
    return bool(s) and not s.startswith("#")


def changed_lines(patch_text, src_root="/sqlglot"):
    """-> (changed {rel: set(old lines)}, structural reason or None).

    '-' lines count as changed; a pure insertion counts its nearest non-blank, non-comment old
    neighbours above and below. Created/deleted/renamed sqlglot files and patched harness files
    are "structural" (the map can't describe them)."""
    changed = {}
    reason = None
    for fe in runner.parse_patch(patch_text):
        op, old, new = fe["op"], fe["old"], fe["new"]
        for p in (old, new):
            if p and _is_harness(p):
                reason = reason or f"harness file {p} patched"
        if op != "modify":
            for p in (old, new):
                if p and p.startswith("sqlglot/"):
                    reason = reason or f"{op} of {p}"
            continue
        if not old.startswith("sqlglot/"):
            continue
        if not old.endswith(".py"):
            reason = reason or f"non-.py sqlglot file {old}"
            continue
        src = runner._read_src(src_root, old)
        L = src.split("\n") if src is not None else []
        s = changed.setdefault(old, set())

        def neighbours(o, L=L, s=s):  # insertion before old line o (1-based)
            a = o - 1
            while 1 <= a <= len(L) and not _code_line(L[a - 1]):
                a -= 1
            if a >= 1:
                s.add(a)
            b = o
            while b <= len(L) and not _code_line(L[b - 1]):
                b += 1
            if b <= len(L):
                s.add(b)

        for o_start, o_len, _ns, _nl, body in fe["hunks"]:
            o = o_start if o_len else o_start + 1
            for op_, _t, _ in body:
                if op_ == " ":
                    o += 1
                elif op_ == "-":
                    s.add(o)
                    o += 1
                else:
                    neighbours(o)
    return changed, reason


CALL_OVERHEAD_S = 0.2  # fork + patched sqlglot/test-module import, measured per verify call


def estimate_cpu(querymap, which, indices, cold=False):
    """Expected verify cpu for these indices from bench_partial's per-query numbers (None if absent).
    cold=True: the server's row/duckdb caches are empty for these queries (first verify of them in
    this process); warm numbers are about half for tpcds. Each measured number includes one
    CALL_OVERHEAD_S."""
    c = querymap[which].get("cpu_cold" if cold else "cpu") or querymap[which].get("cpu")
    if not c:
        return None
    return CALL_OVERHEAD_S + sum(max(c[i] - CALL_OVERHEAD_S, 0.0) for i in indices if 0 <= i < len(c))


def analyze(patch_text, querymap, which, src_root="/sqlglot"):
    try:
        changed, reason = changed_lines(patch_text, src_root)
    except runner.PatchError as e:
        return {"indices": list(range(querymap[which]["n"])), "touches_setup": True, "reason": f"patch: {e}"}
    idx, touches = queries_touching(querymap, which, changed)
    if reason:
        touches = True
    elif touches:
        reason = "setup line changed"
    n = querymap[which]["n"]
    return {"indices": idx, "touches_setup": touches, "reason": reason,
            "est_cpu": estimate_cpu(querymap, which, range(n) if touches else idx)}


# ------------------------------------------------------------------ fork server

_SERVER = r'''
import gc, json, os, resource, signal, sys
tree = sys.argv[1]
sys.path[:] = [tree] + [p for p in sys.path if p not in ("", ".", tree)]
for m in ("ast", "csv", "gzip", "datetime", "pickle", "traceback", "tempfile", "importlib", "unittest",
          "unittest.mock", "concurrent.futures", "concurrent.futures.process", "multiprocessing",
          "multiprocessing.context", "numpy", "pandas", "pandas.testing", "duckdb"):
    try:
        __import__(m)
    except BaseException:
        pass
import importlib
gc.collect()
gc.freeze()

DUCK = {}  # pristine duckdb SQL text -> DataFrame, computed in the server, kept across calls
ROWS = {}  # csv path -> rows, parsed exactly like setUpClass (no sqlglot involved), kept across calls


def parse_rows(file_name):
    import ast, csv, gzip
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    fh = gzip.open(file_name, "rt", newline="") if gzipped else open(file_name, encoding="utf-8", newline="")
    with fh:
        reader = csv.reader(fh, delimiter="|")
        rows = []
        ctypes = []
        next(reader)
        for row in reader:
            if not ctypes:
                for i, v in enumerate(row):
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return rows


def send(o):
    os.write(1, (json.dumps(o) + "\n").encode())


def cpu(ru):
    return ru.ru_utime + ru.ru_stime


def work(req):
    import pickle
    import duckdb
    which, indices = req["which"], req["indices"]
    import sqlglot
    if not os.path.abspath(sqlglot.__file__).startswith(tree + os.sep):
        raise RuntimeError("sqlglot imported from outside the run tree")
    from sqlglot import find_tables, parse_one
    from sqlglot.executor.table import Table
    import tests.test_executor as te
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    TE = te.TestExecutor
    tpch = which in ("tpch", "tpch_opt")
    if tpch:
        schema, directory = TPCH_SCHEMA, te.DIR_TPCH
        sqls = TE.tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    else:
        schema, directory = TPCDS_SCHEMA, te.DIR_TPCDS
        sqls = TE.tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    TE.cache = {}
    TE.tpch_conn = TE.tpcds_conn = None  # cached_execute reads it even on a cache hit
    conn = [None]
    views = set()

    def get_conn(names):
        if conn[0] is None:
            conn[0] = duckdb.connect()
            if tpch:
                TE.tpch_conn = conn[0]
            else:
                TE.tpcds_conn = conn[0]
        for table in names:  # same statement as setUpClass
            if table in views or table not in schema:
                continue
            views.add(table)
            conn[0].execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{directory}{table}.csv.gz', delim='|', header=True, columns={schema[table]})
                """
            )
        return conn[0]

    def load(table):  # setUpClass's python-side loader (rows pre-parsed by the server if asked)
        rows = ROWS.get(req["fixtures"] + table + ".csv.gz")
        if rows is None:
            rows = parse_rows(f"{directory}{table}.csv.gz")
        # Table has no pickle hooks, so the initargs pickle round trip runs no sqlglot code: skipped
        return Table(columns=schema[table], rows=rows)

    class Tables(dict):
        def __missing__(self, name):
            if name not in schema:
                raise KeyError(name)
            v = self[name] = load(name)
            return v

    from sqlglot import transpile

    def duck_df(sql):
        # what cached_execute does; the PATCHED transpile decides the duckdb SQL, and only when it
        # equals pristine output is the server's precomputed result used
        q = transpile(sql, write="duckdb")[0]
        df = DUCK.get(q)
        if df is not None:
            return df.copy()
        return get_conn([t.name for t in find_tables(parse_one(sql))]).execute(q).fetchdf()

    inst = TE("test_execute_tpch" if tpch else "test_execute_tpcds")
    if which == "tpch_opt":
        from pandas.testing import assert_frame_equal
        for i in indices:
            _, sql, optimized = sqls[i]
            TE.cache[sql] = duck_df(sql)
            a = inst.cached_execute(sql, tpch=True)
            b = duck_df(optimized)
            inst.rename_anonymous(b, a)
            assert_frame_equal(a, b)
        return
    # _mp_execute: parse everything in the parent, then results in order
    exprs = [(i, parse_one(sqls[i][1])) for i in indices]
    te.initializer(*pickle.loads(pickle.dumps((schema, {}))))
    te._tables = Tables()
    for i, expr in exprs:
        meta = sqls[i][0]
        fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expr, meta))))
        table = pickle.loads(pickle.dumps(fn(*args)))
        if table is not None:
            sql = sqls[i][1]
            if sql not in TE.cache:
                TE.cache[sql] = duck_df(sql)
            inst.subtestHelper(i, table, tpch=tpch)


def child(req):
    os.setsid()
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(int(req["timeout"]) + 5)
    fd = os.open(os.devnull, os.O_RDWR)
    for k in (0, 1, 2):
        os.dup2(fd, k)
    os.makedirs(req["tmp"], exist_ok=True)
    os.environ["TMPDIR"] = req["tmp"]
    import tempfile
    tempfile.tempdir = None
    os.chdir(tree)
    sys.dont_write_bytecode = bool(req["dwb"])
    for name in list(sys.modules):
        if name.split(".", 1)[0] in ("sqlglot", "tests"):
            del sys.modules[name]
    importlib.invalidate_caches()
    res = {"failed": False, "error": None}
    try:
        work(req)
    except BaseException as e:
        import traceback
        res = {"failed": True, "error": f"{type(e).__name__}: {str(e)[:300]}",
               "tb": traceback.format_exc(limit=-4)[-1500:]}
    tmp = req["out"] + ".part"
    with open(tmp, "w") as f:
        json.dump(res, f)
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
    for t in req.get("prefetch", ()):
        path = req["fixtures"] + t + ".csv.gz"
        if path not in ROWS:
            try:
                ROWS[path] = parse_rows(path)
            except Exception:
                pass
    todo = [(q, ts) for q, ts in req.get("duck", ()) if q not in DUCK]
    if todo:
        try:
            import duckdb
            con = duckdb.connect()
            made = set()
            for q, ts in todo:
                for t in ts:
                    if t not in made:
                        made.add(t)
                        con.execute(f"CREATE VIEW {t} AS SELECT * FROM READ_CSV('{req['fixtures']}{t}.csv.gz', "
                                    f"delim='|', header=True, columns={req['schema'][t]})")
                DUCK[q] = con.execute(q).fetchdf()
        except Exception:
            pass
        finally:
            try:
                con.close()  # no duckdb threads alive across the fork
            except Exception:
                pass
            con = None
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


class _Server(runner._Zygote):
    """runner's zygote plumbing (pipe protocol, read/send/close) with our own server script."""

    def __init__(self, src_root, dwb):  # deliberately not calling super().__init__
        self.src_root, self.preload, self.dwb = src_root, False, dwb
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["SKIP_INTEGRATION"] = "0"
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            env.setdefault(k, "1")
        if dwb:
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        else:
            env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.setdefault("HOME", "/tmp")
        self.buf = b""
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _SERVER, TREE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd="/", bufsize=0, close_fds=True,
        )
        msg = self.read(120.0)
        if not msg or not msg.get("ready"):
            self.close()
            raise RuntimeError("partial server failed to start")
        self.startup_cpu = msg["cpu"]


_S = None
STATS = {"calls": 0, "server_starts": 0, "startup_cpu_s": 0.0}


def _server(src_root):
    global _S
    if _S is not None and (_S.src_root != src_root or _S.proc.poll() is not None):
        _S.close()
        _S = None
    if _S is None:
        info = runner._src_info(src_root)
        os.makedirs(PART_DIR, exist_ok=True)
        _S = _Server(src_root, dwb=info["prebuilt"])
        STATS["server_starts"] += 1
        STATS["startup_cpu_s"] += _S.startup_cpu
        return _S, _S.startup_cpu
    return _S, 0.0


def _prefetch(which, indices):
    """Tables the (pristine) queries read, from the querymap; parsed once in the server."""
    try:
        tl = load_querymap()[which].get("tables")
    except Exception:
        return []
    if not tl:
        return []
    return sorted({t for i in indices if 0 <= i < len(tl) for t in tl[i]})


def _duck(which, indices):
    """Pristine duckdb SQL (+ tables) of these queries, so the server can run each once."""
    try:
        qm = load_querymap()
        dl = qm[which].get("duck")
        schema = qm.get("schemas", {}).get("tpcds" if which == "tpcds" else "tpch")
    except Exception:
        return {}
    if not dl or not schema:
        return {}
    items = [x for i in indices if 0 <= i < len(dl) for x in dl[i]]
    tabs = {t for _, ts in items for t in ts}
    return {"duck": items, "schema": {t: schema[t] for t in tabs if t in schema}}


def _cpu(who):
    ru = resource.getrusage(who)
    return ru.ru_utime + ru.ru_stime


def verify(patch_text, which, query_indices, src_root="/sqlglot", timeout_s=60):
    global _S
    t0 = time.monotonic()
    s0, c0 = _cpu(resource.RUSAGE_SELF), _cpu(resource.RUSAGE_CHILDREN)
    STATS["calls"] += 1
    extra = [0.0]

    def finish(ok, failed, error, **kw):
        cpu = extra[0] + (_cpu(resource.RUSAGE_SELF) - s0) + (_cpu(resource.RUSAGE_CHILDREN) - c0)
        runner._rmtree(TREE)
        runner._rmtree(PART_DIR + "/tmp")
        r = {"ok": ok, "failed": failed, "cpu_s": cpu, "error": error, "wall_s": time.monotonic() - t0}
        r.update(kw)
        return r

    if which not in TEST_IDS:
        return finish(False, False, f"unknown suite {which!r}")
    indices = sorted({int(i) for i in query_indices})
    try:
        changes = runner.apply_patch(patch_text, src_root)
    except (runner.PatchError, ValueError, UnicodeError) as e:
        return finish(False, False, f"patch: {e}")
    bad = [r for r in changes if _is_harness(r)]
    if bad:
        return finish(False, False, f"harness file patched: {bad[0]}")
    if not indices:
        return finish(True, False, None)
    out = PART_DIR + "/result.json"
    pid = None
    try:
        s, startup = _server(src_root)
        extra[0] += startup
        runner.build_tree(src_root, changes, tree=TREE)
        runner._rmtree(out)
        fixtures = os.path.join(src_root, "tests/fixtures/optimizer", "tpc-ds/" if which == "tpcds" else "tpc-h/")
        s.send({"which": which, "indices": indices, "out": out, "tmp": PART_DIR + "/tmp",
                "timeout": timeout_s, "dwb": s.dwb, "fixtures": fixtures,
                "prefetch": _prefetch(which, indices), **_duck(which, indices)})
        # the server first fills its row/duckdb caches (bounded, reused by later calls), then forks;
        # timeout_s applies to the child only
        msg = s.read(max(120.0, 2.0 * timeout_s))
        if not msg or "pid" not in msg:
            raise RuntimeError("server did not fork")
        pid = msg["pid"]
        deadline = time.monotonic() + timeout_s
        msg = s.read(max(0.0, deadline - time.monotonic()))
        timed_out = msg is None
        if timed_out:
            runner._kill_group(pid)
            msg = s.read(30.0)
            if msg is None:
                raise RuntimeError("server stuck after kill")
        extra[0] += msg["cpu"] + msg["zcpu"]
        if timed_out:
            return finish(False, False, f"timeout after {timeout_s}s")
        try:
            with open(out) as f:
                res = json.load(f)
        except (OSError, ValueError):
            return finish(False, False, f"child produced no result (wait status {msg.get('status')})")
        return finish(True, bool(res["failed"]), res.get("error"), tb=res.get("tb"))
    except Exception as e:  # server broken: restart on next call
        if pid:
            runner._kill_group(pid)
        if _S is not None:
            _S.close()
            _S = None
        return finish(False, False, f"partial: {type(e).__name__}: {e}")


def stats():
    return dict(STATS)


def shutdown():
    global _S
    if _S is not None:
        _S.close()
        _S = None
    runner._rmtree(PART_DIR)


import atexit  # noqa: E402

atexit.register(shutdown)
