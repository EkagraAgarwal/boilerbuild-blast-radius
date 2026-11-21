"""Blast Radius selector.

For each patch: find the lines it changes in the pinned sqlglot, map them to the tests
that executed those lines (per-test coverage built once at the pinned commit), add
fallbacks for what line coverage can't see, then pack the chosen tests onto workers.
"""

import base64
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
HERE = Path(__file__).resolve().parent
MAP = Path(os.environ.get("BR_MAP", HERE / "map.json.gz"))
SRC = Path(os.environ.get("BR_SRC", "/sqlglot"))
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- map

class Map:
    def __init__(self, path):
        m = json.loads(gzip.decompress(path.read_bytes()))
        self.n = m["n"]
        self._sets_b64 = m["sets"]
        self._sets = {}
        self.lines = m["lines"]
        self.imp = {k: set(v) for k, v in m["imp"].items()}
        self.opens = m["opens"]

    def set(self, i):
        s = self._sets.get(i)
        if s is None:
            b = base64.b64decode(self._sets_b64[i])
            s = {(j << 3) | k for j, byte in enumerate(b) if byte for k in range(8) if byte >> k & 1}
            self._sets[i] = s
        return s

    def line_tests(self, rel, ln):
        d = self.lines.get(rel)
        if not d:
            return None
        i = d.get(str(ln))
        return None if i is None else self.set(i)

    def file_tests(self, rel):
        out = set()
        for i in set((self.lines.get(rel) or {}).values()):
            out |= self.set(i)
        return out

    def range_tests(self, rel, lo, hi):
        out = set()
        d = self.lines.get(rel) or {}
        for ln in range(lo, hi + 1):
            i = d.get(str(ln))
            if i is not None:
                out |= self.set(i)
        return out


# ---------------------------------------------------------------- patch parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return {key: {"old": path|None, "new": path|None, "changed": set(old line numbers removed),
    "ins": set(old line after which lines were inserted), "added": [...], "removed": [...],
    "binary": bool}}. Handles git headers (new/deleted/renamed/binary) and counts hunk lines,
    so content lines that look like headers ("--- x" removing an SQL comment) are not misread."""
    files = []
    cur = None
    old_rem = new_rem = 0
    old_ln = 0
    pending_old = None

    def new_entry(old, new):
        e = {"old": old, "new": new, "changed": set(), "ins": set(), "added": [], "removed": [],
             "binary": False}
        files.append(e)
        return e

    def strip(p, pre):
        p = p.split("\t")[0].strip()
        if p == "/dev/null":
            return None
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        return p[2:] if p.startswith(pre) else p

    for line in text.replace("\r\n", "\n").split("\n"):
        if old_rem > 0 or new_rem > 0:
            c = line[:1]
            if c == "\\":
                continue
            if c == "-" :
                cur["changed"].add(old_ln)
                cur["removed"].append(line[1:])
                old_ln += 1
                old_rem -= 1
                continue
            if c == "+":
                cur["ins"].add(old_ln - 1)
                cur["added"].append(line[1:])
                new_rem -= 1
                continue
            if c == " " or line == "":
                old_ln += 1
                old_rem -= 1
                new_rem -= 1
                continue
            old_rem = new_rem = 0  # malformed hunk: fall through to header parsing
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', line)
            cur = new_entry(m.group(1), m.group(2)) if m else new_entry(None, None)
            pending_old = cur["old"]
            continue
        if cur is not None and line.startswith("deleted file mode"):
            cur["new"] = None
            continue
        if cur is not None and line.startswith("new file mode"):
            cur["old"] = None
            continue
        if cur is not None and line.startswith("rename from "):
            cur["old"] = line[len("rename from "):]
            continue
        if cur is not None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
            continue
        if cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur["binary"] = True
            continue
        if line.startswith("--- "):
            pending_old = strip(line[4:], "a/")
            continue
        if line.startswith("+++ "):
            newp = strip(line[4:], "b/")
            if cur is None or cur["changed"] or cur["ins"]:
                cur = new_entry(pending_old, newp)
            else:
                cur["old"], cur["new"] = pending_old, newp
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            old_ln = int(m.group(1))
            old_rem = int(m.group(2)) if m.group(2) is not None else 1
            new_rem = int(m.group(4)) if m.group(4) is not None else 1
            if old_rem == 0:
                old_ln += 1  # "-N,0" means insertion after line N
            continue
    out = {}
    for e in files:
        if e["old"] is None and e["new"] is None:
            continue
        key = e["new"] or e["old"]
        if key in out:  # same file in two sections: merge
            o = out[key]
            o["changed"] |= e["changed"]
            o["ins"] |= e["ins"]
            o["added"] += e["added"]
            o["removed"] += e["removed"]
        else:
            out[key] = e
    return out


# ---------------------------------------------------------------- python structure helpers

_src_cache = {}


def source_lines(rel):
    if rel not in _src_cache:
        p = SRC / rel
        try:
            _src_cache[rel] = p.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        except OSError:
            _src_cache[rel] = None
    return _src_cache[rel]


def enclosing_def(lines, ln):
    """Walk up from line ln (1-based) to the nearest def/class with smaller indent.
    Returns (kind, start, end) or None."""
    if not lines or ln < 1 or ln > len(lines):
        return None
    target = lines[ln - 1]
    ind = len(target) - len(target.lstrip()) if target.strip() else 10**6
    for i in range(ln - 1, -1, -1):
        s = lines[i]
        st = s.lstrip()
        if not st:
            continue
        si = len(s) - len(st)
        if si < ind and (st.startswith("def ") or st.startswith("async def ") or st.startswith("class ")):
            # find block end
            end = len(lines)
            for j in range(i + 1, len(lines)):
                t = lines[j]
                if t.strip() and (len(t) - len(t.lstrip())) <= si and not t.lstrip().startswith((")", "]", "}")):
                    end = j
                    break
            return ("class" if st.startswith("class ") else "def", i + 1, end)
        if si < ind:
            ind = si
    return None


def is_trivial(text):
    t = text.strip()
    return t == "" or t.startswith("#")


def apply_hunks(old_lines, patch_text, rel):
    """Rebuild the new version of `rel` from the old lines and the patch's hunks for it.
    Returns list of new lines or None if it can't."""
    out = []
    pos = 0  # 0-based index into old_lines
    in_file = False
    lines = patch_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            p = p[2:] if p.startswith("b/") else p
            in_file = p == rel
            i += 1
            continue
        if in_file:
            m = HUNK.match(line)
            if m:
                start = int(m.group(1)) - 1 if int(m.group(2) or 1) else int(m.group(1))
                if start < pos:
                    return None
                out.extend(old_lines[pos:start])
                pos = start
                i += 1
                while i < len(lines):
                    l = lines[i]
                    if l.startswith(" "):
                        out.append(l[1:])
                        pos += 1
                    elif l.startswith("-") and not l.startswith("--- "):
                        pos += 1
                    elif l.startswith("+") and not l.startswith("+++ "):
                        out.append(l[1:])
                    elif l.startswith("\\"):
                        pass
                    elif l == "" and i + 1 < len(lines) and lines[i + 1].startswith((" ", "-", "+")) \
                            and not lines[i + 1].startswith(("--- ", "+++ ")):
                        out.append("")
                        pos += 1
                    else:
                        break
                    i += 1
                continue
            if line.startswith("diff --git"):
                in_file = False
        i += 1
    out.extend(old_lines[pos:])
    return out


def ast_same(old_text, new_text):
    """True/False if both parse and ASTs (without positions) are equal / differ.
    'syntax' if the new text doesn't parse."""
    import ast
    try:
        new = ast.dump(ast.parse(new_text))
    except SyntaxError:
        return "syntax"
    try:
        old = ast.dump(ast.parse(old_text))
    except SyntaxError:
        return False
    return old == new


# ---------------------------------------------------------------- statement-level expansion

_stmt_cache = {}


def stmt_index(rel, src):
    """List of (lo, hi, kind, node_lo, node_hi) for every statement in the old file.
    For compound statements (lo, hi) is the header only (incl. decorators); node_* is the whole node."""
    import ast
    if rel in _stmt_cache:
        return _stmt_cache[rel]
    out = []
    try:
        tree = ast.parse("\n".join(src or []))
    except SyntaxError:
        tree = None
    def visit(node, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                lo, end = child.lineno, child.end_lineno or child.lineno
                body = getattr(child, "body", None)
                kind = type(child).__name__
                if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                    hi = max(lo, body[0].lineno - 1)
                    decs = getattr(child, "decorator_list", None)
                    if decs:
                        lo = min(lo, min(d.lineno for d in decs))
                else:
                    hi = end
                out.append((lo, hi, kind, child.lineno, end, in_func))
                visit(child, in_func or kind in ("FunctionDef", "AsyncFunctionDef", "Lambda"))
            else:
                visit(child, in_func)

    if tree is not None:
        visit(tree, False)
    _stmt_cache[rel] = out
    return out


def innermost(stmts, ln):
    best = None
    for s in stmts:
        if s[0] <= ln <= s[1] and (best is None or (s[1] - s[0]) < (best[1] - best[0])):
            best = s
    return best


_all_src = None


def name_users(mp, name):
    """Tests that executed any sqlglot line mentioning identifier `name`."""
    global _all_src
    if _all_src is None:
        _all_src = {}
        for rel in mp.lines:
            if rel.endswith(".py"):
                _all_src[rel] = source_lines(rel) or []
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    out = set()
    for rel, lines in _all_src.items():
        d = mp.lines.get(rel) or {}
        for i, l in enumerate(lines, 1):
            if name in l and pat.search(l):
                k = d.get(str(i))
                if k is not None:
                    out |= mp.set(k)
    return out


def stem_users(mp, rel):
    """Tests that executed code in any module sharing this file's stem (dialect families)."""
    stem = Path(rel).stem
    out = set()
    if stem in ("__init__", "dialect", "parser", "generator", "tokens", "tokenizer"):
        return None
    for r in mp.lines:
        if r.startswith("sqlglot/") and Path(r).stem == stem:
            out |= mp.file_tests(r)
    return out


TESTS_BY_FILE = {}
FUNC_WIDEN = os.environ.get("BR_FUNC_WIDEN", "1") == "1"
ASSIGN_NAME = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
IMPORT_NAMES = re.compile(r"^\s*(?:from\s+\S+\s+)?import\s+(.+)$")


def line_candidates(mp, rel, src, stmts, ln):
    """Tests that could observe a change at old line ln of rel."""
    s = innermost(stmts, ln)
    if s is None:
        return set(), None  # blank / comment between statements
    lo, hi, kind, nlo, nhi, in_func = s
    t = mp.range_tests(rel, lo, hi)
    if kind in ("FunctionDef", "AsyncFunctionDef", "ClassDef"):
        # signature / decorator / bases change: everything that runs the body
        return mp.range_tests(rel, nlo, nhi), f"header:{kind}"
    if in_func:
        enc = enclosing_def(src, ln)
        if enc and enc[0] == "def":
            head = "\n".join(src[max(0, enc[1] - 4):enc[1]])
            fname = (re.search(r"def\s+(\w+)", src[enc[1] - 1]) or [None, ""])[1]
            if fname == "__getattr__" or re.search(r"@(functools\.)?(lru_cache|cache|cached_property)\b", head):
                # runs once and is cached: coverage only credits the first caller
                pkg = rel.rsplit("/", 1)[0] + "/"
                wide = set(t) | mp.file_tests(rel) | name_users(mp, fname)
                if rel.endswith("__init__.py"):
                    for r in mp.lines:
                        if r.startswith(pkg):
                            wide |= mp.file_tests(r)
                return wide, f"cached:{fname}"
        if FUNC_WIDEN and enc and enc[0] == "def":
            # a change can make unreached code in the same function reachable: everyone who ran it
            return set(t) | mp.range_tests(rel, enc[1], enc[2]), None
        return t, None
    imp = mp.imp.get(rel, set())
    if not t and not any(i in imp for i in range(lo, hi + 1)):
        return set(), None  # never executed (dead code)
    # module-level or class-body statement: runs once at (possibly lazy) import, so line coverage
    # only credits whichever test imported the module first
    t = set(t)
    text = "\n".join(src[lo - 1:hi])
    names = set()
    m = ASSIGN_NAME.match(src[lo - 1])
    if m:
        names.add(m.group(1))
    m = IMPORT_NAMES.match(src[lo - 1])
    if m:
        for part in m.group(1).replace("(", " ").replace(")", " ").split(","):
            w = part.strip().split(" as ")[-1].strip()
            if w.isidentifier():
                names.add(w)
    users = set()
    for n in names:
        users |= name_users(mp, n)
    if rel.startswith("tests/") and names:
        # names used at import time by test modules (e.g. skipIf decorators): their whole files
        pats = [re.compile(r"\b" + re.escape(n) + r"\b") for n in names]
        for fl, ks in TESTS_BY_FILE.items():
            if fl == rel:
                continue
            txt = "\n".join(source_lines(fl) or [])
            if any(p.search(txt) for p in pats):
                users |= set(ks)
    fam = stem_users(mp, rel) if not rel.startswith("tests/") else None
    if users and fam:
        cand = users & fam
        if cand:
            return cand | t, f"importtime[{','.join(sorted(names))}&fam]"
    if users:
        return users | t, f"importtime[{','.join(sorted(names))}]"
    if fam:
        return fam | t, "importtime[fam]"
    return mp.file_tests(rel) | t, "importtime[file]"


# ---------------------------------------------------------------- selection

def candidates(mp, files, tests_by_file, patch_text, all_tests):
    sel = set()
    reasons = []
    for rel, f in files.items():
        old = f["old"]
        name = (f["new"] or old or "").rsplit("/", 1)[-1]
        if name == "conftest.py" or (old and old.rsplit("/", 1)[-1] == "conftest.py"):
            d = (f["new"] or old).rsplit("/", 1)[0] + "/" if "/" in (f["new"] or old) else ""
            hit = {k for fl, ks in tests_by_file.items() if fl.startswith(d) for k in ks}
            sel |= hit
            reasons.append(f"conftest {rel} -> {len(hit)}")
            continue
        if old is None:
            # brand new file: a new test file has no numbered tests; a new module only matters if
            # something imports it, and that import is an edit to an existing file handled below
            continue
        if f["new"] is None or (f["new"] != old):
            # deleted or renamed: everything that loaded the old path breaks
            if old.endswith(".py"):
                base = old.rsplit("/", 1)[-1]
                stem = base[:-3]
                if old.startswith("tests/") and base.startswith("test_"):
                    hit = set(tests_by_file.get(old, []))
                elif old.startswith("tests/"):
                    # shared test helper: every test module importing it
                    mod = old[:-3].replace("/", ".")
                    hit = mp.file_tests(old)
                    for fl, ks in tests_by_file.items():
                        txt = "\n".join(source_lines(fl) or [])
                        if mod in txt or f"import {stem}" in txt or f"from . import" in txt:
                            hit |= set(ks)
                elif old in mp.imp:
                    hit = set(all_tests)
                else:
                    # lazily imported module (e.g. a dialect): its users and its test module
                    hit = mp.file_tests(old) | (stem_users(mp, old) or set())
                    for fl, ks in tests_by_file.items():
                        if fl.rsplit("/", 1)[-1] == f"test_{stem}.py":
                            hit |= set(ks)
            else:
                i = mp.opens.get(old)
                hit = mp.set(i) if i is not None else set()
            sel |= hit
            reasons.append(f"{'deleted' if f['new'] is None else 'renamed'} {old} -> {len(hit)}")
            continue
        if f.get("binary"):
            i = mp.opens.get(old)
            hit = mp.set(i) if i is not None else (mp.file_tests(old) | set(tests_by_file.get(old, [])))
            sel |= hit
            reasons.append(f"binary {old} -> {len(hit)}")
            continue
        if not rel.endswith(".py") and rel.rsplit("/", 1)[-1] in ("pyproject.toml", "setup.cfg", "pytest.ini",
                                                                 "tox.ini"):
            body = "\n".join(f["added"] + f["removed"]).lower()
            if "pytest" in body or "addopts" in body or "testpaths" in body or "filterwarnings" in body:
                sel |= set(all_tests)
                reasons.append(f"pytest-config {rel}")
            continue
        if rel.endswith(".py"):
            oi = mp.opens.get(old)
            if oi is not None and mp.set(oi):
                # some tests read this source file as text (not just import it)
                sel |= mp.set(oi)
                reasons.append(f"read-as-data {old} -> {len(mp.set(oi))}")
            src = source_lines(old)
            if src is not None and f["new"] is not None:
                new = apply_hunks(src, patch_text, f["new"])
                if new is not None:
                    same = ast_same("\n".join(src), "\n".join(new))
                    if same is True:
                        reasons.append(f"ast-same {old}")
                        continue
                    if same == "syntax":
                        reasons.append(f"SYNTAX {old}")
                        if old in mp.imp:
                            sel |= all_tests
                        else:
                            sel |= mp.file_tests(old)
                        continue
                else:
                    reasons.append(f"apply-failed {old}")
            stmts = stmt_index(old, src)
            # names newly defined or assigned by added lines (an override or a new class attribute
            # changes behaviour wherever the base version / the name is used)
            added_names = set()
            for l in f["added"]:
                m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", l) or re.match(r"\s+([A-Z_][A-Z0-9_]+)\s*(?::[^=]+)?=(?!=)", l)
                if m:
                    added_names.add(m.group(1))
            if added_names:
                users = set()
                for n in added_names:
                    users |= name_users(mp, n)
                fam = stem_users(mp, old)
                hit = (users & fam) if (users and fam) else (users or fam or mp.file_tests(old))
                sel |= hit
                reasons.append(f"added[{','.join(sorted(added_names))[:60]}] {old} -> {len(hit)}")
            touched = set(f["changed"]) | set(f["ins"]) | {i + 1 for i in f["ins"]}
            for ln in sorted(touched):
                t, why = line_candidates(mp, old, src, stmts, ln)
                sel |= t
                if why:
                    reasons.append(f"{why} {old}:{ln} -> {len(t)}")
            if old.startswith("tests/") and old in tests_by_file:
                # a test-module edit outside any test body (imports, helpers at import, class attrs,
                # setUpClass) can break every test in that file
                own = set(tests_by_file[old])
                if any(not (mp.line_tests(old, ln)) for ln in f["changed"] if src and ln <= len(src)
                       and not is_trivial(src[ln - 1])):
                    sel |= own
                    reasons.append(f"testfile {old} -> {len(own)}")
        else:
            i = mp.opens.get(old)
            if i is not None:
                sel |= mp.set(i)
                reasons.append(f"opened {old} -> {len(mp.set(i))}")
            else:
                reasons.append(f"unmapped {old}")
    return sel, reasons


def pack(tests, ms):
    """Best LPT split over k = 1..8 workers. Returns (cost_ms, workers)."""
    if not tests:
        return 0, []
    order = sorted(tests, key=lambda t: -ms[t])
    best = None
    for k in range(1, 9):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            loads[i] += ms[t]
            ws[i].append(t)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best


# Running a candidate set ourselves pays off only when it's cheap: optimizer/executor tests cost
# several times their recorded seconds to run (module import, setUpClass, process pools).
EXPENSIVE_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")
RUN_MAX_EST_S = float(os.environ.get("BR_RUN_MAX", "1.0"))
RUN_BUDGET_S = float(os.environ.get("BR_RUN_BUDGET", "60"))
RUN_ENABLED = os.environ.get("BR_RUN", "0") == "1"


def run_estimate(sel, tests_meta):
    est = 0.15
    for k in sel:
        f, sec = tests_meta[k]
        if f in EXPENSIVE_FILES:
            return None
        est += sec * 1.5 + 0.01
    return est


def main():
    t_start = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    meta = {t["number"]: (t["file"], t["seconds"]) for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    mp = Map(MAP)
    TESTS_BY_FILE.update(tests_by_file)

    plans = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        files = parse_patch(text)
        try:
            sel, reasons = candidates(mp, files, tests_by_file, text, set(ms))
        except Exception as e:  # never crash: fall back to everything
            log(f"{patch.stem}: selector error {e!r}; selecting all")
            sel, reasons = set(ms), ["error"]
        sel &= set(ms)
        plans.append([patch.stem, text, sel, reasons])

    eligible = []
    if RUN_ENABLED:
        for p in plans:
            if not p[2] or any(r.startswith(("SYNTAX", "apply-failed", "error")) for r in p[3]):
                continue
            est = run_estimate(p[2], meta)
            if est is not None and est <= RUN_MAX_EST_S:
                eligible.append(p)
    # one-time runner start-up costs ~1-2 s CPU: only worth it with a few cheap commits to verify
    if len(eligible) >= 3:
        try:
            import runner
        except Exception as e:
            log(f"runner unavailable: {e!r}")
            eligible = []
        spent = 0.0
        for p in eligible:
            if time.monotonic() - t_start > 120 or spent > RUN_BUDGET_S:
                break
            res = runner.run_tests(p[1], [ids[k] for k in sorted(p[2])], src_root=str(SRC), timeout_s=15.0)
            spent += res["cpu_s"]
            if res["ok"]:
                failed = {num[i] for i in res["failed"] if i in num}
                p[3].append(f"ran {len(p[2])} -> {len(failed)} failed ({res['cpu_s']:.2f}s)")
                p[2] = failed
            else:
                p[3].append(f"run failed: {res['error']}")
        try:
            runner.shutdown()
        except Exception:
            pass

    for cid, _text, sel, reasons in plans:
        cost, ws = pack(sel, ms)
        log(f"{cid}: {len(sel)} tests, {cost/1000:.1f}s {'; '.join(reasons)[:300]}")
        print(json.dumps({"commit": cid, "workers": ws}), flush=True)


if __name__ == "__main__":
    main()
