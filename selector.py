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

DATA = os.environ.get("BR_DATA", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "map.json.gz"))
SRC = os.environ.get("BR_SRC", "/sqlglot")
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


def _stem(rel):
    b = rel.rsplit("/", 1)[-1]
    return b.rsplit(".", 1)[0] if "." in b else b


# ---------------------------------------------------------------- map

INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "index.marshal"))
_IDX = None


def load_index():
    global _IDX
    if _IDX is None:
        _IDX = {}
        if os.path.exists(INDEX):
            import marshal
            with open(INDEX, "rb") as f:
                _IDX = marshal.load(f)
    return _IDX


_IDX_FILES = {}


def index_file(rel):
    if rel not in _IDX_FILES:
        b = load_index().get("files", {}).get(rel)
        if b is not None:
            import marshal
            b = marshal.loads(b)
        _IDX_FILES[rel] = b
    return _IDX_FILES[rel]


_NAMES = None


def files_with_name(name):
    """Files whose text contains identifier `name` (None if no index)."""
    global _NAMES
    if _NAMES is None:
        b = load_index().get("names")
        if b is None:
            _NAMES = False
        else:
            import marshal
            _NAMES = marshal.loads(b)
    if _NAMES is False:
        return None
    return _NAMES.get(name, ())


class Map:
    def __init__(self, path):
        idx = load_index()
        if idx.get("map"):
            import marshal
            m = marshal.loads(idx["map"])
        else:
            m = json.loads(gzip.decompress(open(path, "rb").read()))
        self.n = m["n"]
        self._sets_b64 = m["sets"]
        self._sets = {}
        self._ints = {}
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

    def has(self, i, k):
        """Is test k in set i? (bit test on a cached int, no set decoding)"""
        v = self._ints.get(i)
        if v is None:
            v = self._ints[i] = int.from_bytes(base64.b64decode(self._sets_b64[i]), "little")
        return (v >> k) & 1 == 1

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
        txt = load_index().get("texts", {}).get(rel)
        if txt is not None:
            _src_cache[rel] = txt.decode("utf-8").split("\n")
            return _src_cache[rel]
        try:
            with open(os.path.join(SRC, rel), encoding="utf-8") as fh:
                _src_cache[rel] = fh.read().replace("\r\n", "\n").split("\n")
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


def fast_same(rel, f, src, new):
    """Like ast_same but avoids parsing/dumping whole files when the edit is clearly
    comment/blank-only, and only syntax-checks the new text when real code changed."""
    pre = index_file(rel)
    trivial = all(is_trivial(l) for l in f["removed"] + f["added"])
    if trivial and pre is not None:
        strl = set(pre["strlines"])
        touch = set(f["changed"]) | set(f["ins"]) | {i + 1 for i in f["ins"]}
        cont = any(1 <= i <= len(src) and src[i - 1].rstrip().endswith("\\") for i in f["ins"])
        if not (touch & strl) and not cont:
            return True
    norm_old = [l.strip() for l in f["removed"] if not is_trivial(l)]
    norm_new = [l.strip() for l in f["added"] if not is_trivial(l)]
    if trivial or "".join(norm_old).replace(" ", "") == "".join(norm_new).replace(" ", ""):
        return ast_same("\n".join(src), "\n".join(new))  # reformatting: compare properly
    try:
        compile("\n".join(new), rel, "exec", 0x400, dont_inherit=True)  # PyCF_ONLY_AST
    except SyntaxError:
        return "syntax"
    except ValueError:
        return False
    if EQUIV and _equiv_candidate(f):
        try:
            if equiv_same("\n".join(src), "\n".join(new)):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


EQUIV = os.environ.get("BR_EQUIV", "1") == "1"
TABLE_ON = os.environ.get("BR_TABLE", "1") == "1"
TABLE_PATH = os.environ.get("BR_TABLE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "table.marshal"))
_table = []


def table_lookup(rel, f):
    """Precomputed single-token mutants: (file, old line no, new line) -> exact failing tests.
    Only for an edit that replaces exactly one code line (other added lines must be comments)."""
    rem = [l for l in f["removed"] if not is_trivial(l)]
    add = [l for l in f["added"] if not is_trivial(l)]
    if len(rem) != 1 or len(add) != 1 or len(f["changed"]) != 1 or len(f["removed"]) != 1:
        return None
    if any(l.strip() and not l.strip().startswith("#") for l in f["added"] if l is not add[0]):
        return None
    if not _table:
        try:
            import marshal
            with open(TABLE_PATH, "rb") as fh:
                _table.append(marshal.load(fh))
        except Exception:  # noqa: BLE001
            _table.append(({}, []))
    keys, sets = _table[0]
    (ln,) = f["changed"]
    v = keys.get(f"{rel}\t{ln}\t{add[0]}")
    if v is None or v[0] != rem[0]:
        return None
    b = int.from_bytes(sets[v[1]], "little")
    out = set()
    i = 0
    while b:
        if b & 1:
            out.add(i)
        b >>= 1
        i += 1
    return out
_EQ_HINT = re.compile(r"\bis\b|==|!=")


def _equiv_candidate(f):
    return any(_EQ_HINT.search(l) for l in f["removed"]) and any(_EQ_HINT.search(l) for l in f["added"])


def _singleton(node):
    """None, or an enum-style member Foo.BAR / mod.Foo.BAR (identity == equality for these)."""
    import ast
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Attribute) and node.attr.isupper():
        v = node.value
        base = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else ""
        return base in _ENUMS  # only real Enum classes of the pinned sqlglot (not e.g. exp.UnixToTime.MICROS)
    return False


_ENUMS = {"TokenType", "DType", "Type", "ScopeType", "ErrorLevel", "TrieResult", "Location", "PropertiesLocation",
          "AliasPrecedence", "NormalizationStrategy", "IsPredicatePrecedence", "UsingColumnOrder", "Dialects"}


def equiv_same(old_text, new_text):
    """ASTs equal after treating `is`/`is not` as `==`/`!=` when compared with None or an enum member."""
    import ast

    def norm(text):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                ops = list(node.ops)
                seq = [node.left] + list(node.comparators)
                for i, op in enumerate(ops):
                    if isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) and (
                            _singleton(seq[i]) or _singleton(seq[i + 1])):
                        ops[i] = ast.Eq() if isinstance(op, (ast.Is, ast.Eq)) else ast.NotEq()
                node.ops = ops
        return ast.dump(tree)

    return norm(old_text) == norm(new_text)


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
    pre = index_file(rel)
    if pre is not None:
        _stmt_cache[rel] = [tuple(x) for x in pre["stmts"]]
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
_all_txt = {}


def name_users(mp, name):
    """Tests that executed any sqlglot line mentioning identifier `name`."""
    global _all_src
    fl = files_with_name(name)
    if fl is not None:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        out = set()
        for rel in fl:
            d = mp.lines.get(rel)
            if not d:
                continue
            for i, l in enumerate(source_lines(rel) or [], 1):
                if name in l and pat.search(l):
                    k = d.get(str(i))
                    if k is not None:
                        out |= mp.set(k)
        return out
    if _all_src is None:
        _all_src = {}
        for rel in mp.lines:
            if rel.endswith(".py"):
                _all_src[rel] = source_lines(rel) or []
                _all_txt[rel] = "\n".join(_all_src[rel])
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    out = set()
    for rel, lines in _all_src.items():
        if name not in _all_txt[rel]:
            continue
        d = mp.lines.get(rel) or {}
        for i, l in enumerate(lines, 1):
            if name in l and pat.search(l):
                k = d.get(str(i))
                if k is not None:
                    out |= mp.set(k)
    return out


def stem_users(mp, rel):
    """Tests that executed code in any module sharing this file's stem (dialect families)."""
    stem = _stem(rel)
    out = set()
    if stem in ("__init__", "dialect", "parser", "generator", "tokens", "tokenizer"):
        return None
    for r in mp.lines:
        if r.startswith("sqlglot/") and _stem(r) == stem:
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
    # class-body statement: every test that touched the class itself (e.g. exp.Variance users)
    cls_users = set()
    best = None
    for c in stmts:
        if c[2] == "ClassDef" and c[3] < lo and hi <= c[4] and (best is None or c[3] > best[3]):
            best = c
    family_module = rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/",
                                    "sqlglot/tokenizers/", "sqlglot/typing/"))
    if best is not None and not family_module:
        m = re.match(r"\s*class\s+(\w+)", src[best[3] - 1])
        if m:
            cls_users = name_users(mp, m.group(1)) | mp.range_tests(rel, best[3], best[4])
            t = t | cls_users
            if "arg_types" in names and m.group(1) in IMPORT_PARSED:
                # the DuckDB generator parses SQL in its class body: a newly required arg of a class
                # built there makes `import sqlglot.dialects.duckdb` raise for every DuckDB user
                t = t | duckdb_users(mp)
    # the module's own test file (dialects/dremio.py -> tests/dialects/test_dremio.py)
    own = f"test_{_stem(rel)}.py"
    for fl, ks in TESTS_BY_FILE.items():
        if fl.rsplit("/", 1)[-1] == own:
            t = t | set(ks)
    if fam is not None and IMPORT_NAMES.match(src[lo - 1]):
        # an edited/removed import of a dialect-family module can make the whole module fail to
        # import (NameError in its class bodies): every test naming a dialect that loads it breaks
        dep = dialect_dependents(rel)
        return fam | dep | t, f"importtime-import[{','.join(sorted(names))}|fam+{len(dep)}]"
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
    # the table answers one mutated line exactly; two mutated code files can interact, so then
    # fall back to the coverage rules for all of them
    use_table = TABLE_ON and sum(1 for r_, f_ in files.items() if r_.endswith(".py") and f_["old"]
                                 and f_["new"] == f_["old"] and table_lookup(f_["old"], f_) is not None) <= 1
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
        if rel.endswith(".py") and use_table:
            hit = table_lookup(old, f)
            if hit is not None:
                sel |= hit
                reasons.append(f"table {old} -> {len(hit)}")
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
                    same = fast_same(old, f, src, new)
                    if same is True:
                        reasons.append(f"ast-same {old}")
                        continue
                    if same == "syntax":
                        reasons.append(f"SYNTAX {old}")
                        if old.startswith("tests/") and old.rsplit("/", 1)[-1].startswith("test_"):
                            sel |= set(tests_by_file.get(old, []))  # only this test module fails to import
                        else:
                            # lazily imported modules (dialects) are loaded from many test files
                            sel |= all_tests
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
                fam = stem_users(mp, old) or set()
                hit = (users & fam) or (users | fam) or mp.file_tests(old)
                # a new generator handler `foo_sql` changes output for expression class Foo everywhere
                # this generator (or one inheriting from it) is used
                for n in added_names:
                    if n.endswith("_sql"):
                        hit |= expr_class_users(mp, n[:-4])
                sel |= hit
                reasons.append(f"added[{','.join(sorted(added_names))[:60]}] {old} -> {len(hit)}")
            touched = set(f["changed"]) | set(f["ins"]) | {i + 1 for i in f["ins"]}
            for ln in sorted(touched):
                t, why = line_candidates(mp, old, src, stmts, ln)
                sel |= t
                if why:
                    reasons.append(f"{why} {old}:{ln} -> {len(t)}")
            # collection: an import, or function code that runs while pytest imports a test module
            # (lazy exports, metaclasses), can make that module fail to import -> all its tests fail
            cfiles = collection_files(old, stmts, touched)
            if cfiles:
                hit = {k for fl in cfiles for k in tests_by_file.get(fl, [])}
                sel |= hit
                reasons.append(f"collection {old} -> {len(cfiles)} files/{len(hit)}")
            if not old.startswith(("sqlglot/", "tests/")):
                hit = mentioning_tests(old, tests_by_file)
                sel |= hit
                reasons.append(f"repo-file {old} -> {len(hit)}")
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
                hit = mentioning_tests(old, tests_by_file)
                sel |= hit
                reasons.append(f"unmapped {old} -> {len(hit)}")
    return sel, reasons


_COLLECT = None
TEST_NUM = {}
MAP_REF = []
_DIAL = []


def dialect_dependents(rel):
    """Tests naming any dialect whose module (transitively) imports `rel` at load time."""
    if not _DIAL:
        b = load_index().get("dialects")
        if b is None:
            _DIAL.append({"users": {}, "deps": {}})
        else:
            import marshal
            _DIAL.append(marshal.loads(b))
    d = _DIAL[0]
    out = set()
    names = list(d["deps"].get(rel, ()))
    if names:
        names.append("*")
    want = set(names)
    for fx, ds in d.get("fixtures", {}).items():
        if want & set(ds) and MAP_REF:
            oi = MAP_REF[0].opens.get(fx)
            if oi is not None:
                out |= MAP_REF[0].set(oi)
    for name in names:
        for tid in d["users"].get(name, ()):
            k = TEST_NUM.get(tid)
            if k is not None:
                out.add(k)
            else:  # unittest ids may carry parametrisation suffixes
                out.update(v for i, v in TEST_NUM.items() if i.startswith(tid + "["))
    return out

# expression classes validated by the parser while `sqlglot.dialects.duckdb` imports (the only
# dialect that parses SQL at import time, in DuckDBGenerator's class body)
IMPORT_PARSED = {
    "Abs", "Add", "Alias", "And", "Anonymous", "ArrayContains", "ArraySize", "Between", "BitwiseAnd",
    "BitwiseOr", "BitwiseRightShift", "Bracket", "CTE", "Case", "Cast", "Coalesce", "Column", "Concat",
    "Cos", "Count", "DPipe", "Distinct", "Div", "EQ", "Encode", "Filter", "From", "GT", "GTE",
    "GenerateSeries", "Greatest", "Group", "If", "Is", "JSONExtract", "JSONKeyValue", "JSONObject", "Join",
    "LT", "LTE", "Lambda", "Length", "List", "Ln", "Lower", "MapFromEntries", "MapKeys", "Min", "Mod", "Mul",
    "NEQ", "Neg", "Not", "Null", "NullSafeEQ", "Or", "Order", "Ordered", "Paren", "Pi", "Placeholder", "Pow",
    "PropertyEQ", "Repeat", "Replace", "RowNumber", "SHA", "Select", "Slice", "Sqrt", "Star", "Sub",
    "Subquery", "Substring", "Sum", "Table", "TableAlias", "Unhex", "Unnest", "Where", "Window", "With"}
_DUCK = []


def duckdb_users(mp):
    """Every test that loads the DuckDB dialect: runtime users of its modules plus every test in a
    test file whose collection imports it."""
    if _DUCK:
        return _DUCK[0]
    out = set()
    for f in ("sqlglot/dialects/duckdb.py", "sqlglot/generators/duckdb.py", "sqlglot/parsers/duckdb.py",
              "sqlglot/tokenizers/duckdb.py", "sqlglot/typing/duckdb.py"):
        out |= set(mp.file_tests(f))
    global _COLLECT
    if _COLLECT is None:
        p = os.path.join(HERE, "collect.json.gz")
        _COLLECT = json.loads(gzip.decompress(open(p, "rb").read())) if os.path.exists(p) else {}
    cl = (_COLLECT.get("lines") or {}).get("sqlglot/generators/duckdb.py") or {}
    files = set()
    for g in set(cl.values()):
        files.update(_COLLECT["files"][i] for i in _COLLECT["groups"][g])
    for fl in files:
        out |= set(TESTS_BY_FILE.get(fl, []))
    _DUCK.append(out)
    return out


def collection_files(rel, stmts, touched):
    """Test files whose collection (module import) executes a risky touched statement."""
    global _COLLECT
    if _COLLECT is None:
        p = os.path.join(HERE, "collect.json.gz")
        _COLLECT = json.loads(gzip.decompress(open(p, "rb").read())) if os.path.exists(p) else {}
    cl = (_COLLECT.get("lines") or {}).get(rel)
    if not cl:
        return set()
    out = set()
    for ln in touched:
        s = innermost(stmts, ln)
        if s is None:
            continue
        lo, hi, kind, nlo, nhi, in_func = s
        if not (in_func or kind in ("Import", "ImportFrom")):
            continue
        for l in range(lo, hi + 1):
            g = cl.get(str(l))
            if g is not None:
                out.update(_COLLECT["files"][i] for i in _COLLECT["groups"][g])
    return out


_EXPR_NAMES = None


def expr_class_users(mp, key):
    """Tests that ran code mentioning the expression class whose lowercase name is `key`."""
    global _EXPR_NAMES
    if _EXPR_NAMES is None:
        _EXPR_NAMES = {}
        for rel in mp.lines:
            if rel.startswith("sqlglot/expressions") or rel == "sqlglot/expressions.py":
                for l in source_lines(rel) or []:
                    m = re.match(r"class (\w+)\(", l)
                    if m:
                        _EXPR_NAMES.setdefault(m.group(1).lower(), set()).add(m.group(1))
    out = set()
    for cname in set(_EXPR_NAMES.get(key.replace("_", ""), ())) | set(_EXPR_NAMES.get(key, ())):
        out |= name_users(mp, cname)
    return out


def mentioning_tests(rel, tests_by_file):
    """Repo files the coverage map doesn't trace (e.g. sqlglotc/setup.py, data files): tests in
    test modules whose source mentions the file's path or its top folder + file name."""
    parts = rel.split("/")
    base = parts[-1]
    top = parts[0] if len(parts) > 1 else None
    out = set()
    for fl, ks in tests_by_file.items():
        txt = "\n".join(source_lines(fl) or [])
        if rel in txt or (top and top in txt and base in txt) or (not top and base in txt):
            out |= set(ks)
    return out


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


# ---------------------------------------------------------------- TPC-DS proxy
# test_execute_tpcds (21.5 s) optimizes the 99 TPC-DS queries and runs them with the Python
# executor. TestOptimizer::test_tpcds (2.2 s) optimizes the same queries and checks the SQL.
# If the patch only touches code both tests run (nothing executor-only, no generators, no
# fixtures), the executor test can only change if the optimized queries change, which the
# optimizer test detects. So run the optimizer test here; if it passes, neither is needed.
EXEC_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
OPT_TPCDS = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
PROXY_ENABLED = os.environ.get("BR_PROXY", "1") == "1"
PROXY_UNSAFE = ("sqlglot/executor/", "sqlglot/generator.py", "sqlglot/generators/", "sqlglot/serde.py",
                "sqlglot/dialects/duckdb.py", "sqlglot/dialects/python.py")


def proxy_eligible(mp, files, reasons, e, o):
    if any(r.startswith(("SYNTAX", "apply-failed", "error", "collection", "cached", "conftest", "deleted",
                         "renamed", "binary", "repo-file", "unmapped", "pytest-config", "read-as-data"))
           for r in reasons):
        return False
    for rel, f in files.items():
        old = f["old"]
        if old is None or f["new"] != old or f.get("binary"):
            return False
        if not old.endswith(".py") or not old.startswith("sqlglot/") or old.startswith(PROXY_UNSAFE):
            return False
        src = source_lines(old)
        stmts = stmt_index(old, src)
        touched = set(f["changed"]) | set(f["ins"]) | {i + 1 for i in f["ins"]}
        d = mp.lines.get(old) or {}
        for ln in touched:
            s_ = innermost(stmts, ln)
            if s_ is None:
                continue
            lo, hi = (s_[3], s_[4]) if s_[2] in ("FunctionDef", "AsyncFunctionDef", "ClassDef") else (s_[0], s_[1])
            if not s_[5] and s_[2] not in ("FunctionDef", "AsyncFunctionDef"):
                return False  # module/class level: import-time effects aren't proven by coverage
            for l in range(lo, hi + 1):
                k = d.get(str(l))
                if k is not None and mp.has(k, e) and not mp.has(k, o):
                    return False  # executor-only code
    return True


def _cg_usec():
    try:
        with open("/sys/fs/cgroup/cpu.stat") as fh:
            for l in fh:
                if l.startswith("usage_usec"):
                    return int(l.split()[1])
    except OSError:
        pass
    return 0


PROXY_CPU_PER_QUERY = 0.12
PROXY_CPU_BASE = 0.6


def proxy_tpcds(plans, mp, num, ids, t_start, ms=None):
    e, o = num.get(EXEC_TPCDS), num.get(OPT_TPCDS)
    if not PROXY_ENABLED or e is None or o is None:
        return
    todo = [p for p in plans if e in p[2] and not any(r.startswith("table ") for r in p[3])
            and proxy_eligible(mp, p[4], p[3], e, o)]
    if not todo:
        return
    try:
        import runner
    except Exception as ex:
        log(f"proxy: runner unavailable {ex!r}")
        return
    import resource
    import shutil
    import subprocess

    script = os.path.join(HERE, "tpcds_proxy.py")
    qm = None
    try:
        import partial as _partial
        qm = json.loads(gzip.decompress(open(os.path.join(HERE, "querymap.json.gz"), "rb").read()))
    except Exception as ex:  # noqa: BLE001
        log(f"proxy: no query map ({ex!r}); replaying all queries")
    for p in todo:
        subset = ""
        nq = 99
        if qm is not None:
            changed = {}
            for rel, f in p[4].items():
                src = source_lines(f["old"])
                stmts = stmt_index(f["old"], src)
                touched = set(f["changed"]) | set(f["ins"]) | {i + 1 for i in f["ins"]}
                lines = changed.setdefault(f["old"], set())
                for ln in touched:
                    s_ = innermost(stmts, ln)
                    if s_ is None:
                        continue
                    lo, hi = (s_[3], s_[4]) if s_[2] in ("FunctionDef", "AsyncFunctionDef") else (s_[0], s_[1])
                    lines.update(range(lo, hi + 1))
            try:
                q, touches_setup = _partial.queries_touching(qm, "tpcds", changed)
                if not touches_setup and q:
                    subset = ",".join(map(str, q))
                    nq = len(q)
            except Exception as ex:  # noqa: BLE001
                log(f"proxy: query map lookup failed {ex!r}")
        if time.monotonic() - t_start > 110:
            p[3].append("proxy: skipped (time)")
            continue
        if ms is not None:
            saving = (pack(p[2], ms)[0] - pack(p[2] - {e, o}, ms)[0]) / 1000
            est = PROXY_CPU_BASE + PROXY_CPU_PER_QUERY * nq
            if saving <= est:
                p[3].append(f"proxy: not worth it (saving {saving:.1f}s <= est {est:.1f}s)")
                continue
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        g0 = _cg_usec()
        ok = False
        try:
            changes = runner.apply_patch(p[1], str(SRC))
            if os.path.exists(runner.RUN_DIR):
                shutil.rmtree(runner.RUN_DIR, ignore_errors=True)
            os.makedirs(runner.RUN_DIR, exist_ok=True)
            runner.build_tree(str(SRC), changes)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, script, runner.TREE, subset], env=env, timeout=40,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            ok = r.returncode == 0
            why = "passed" if ok else (r.stderr.strip().splitlines() or ["failed"])[-1][:80]
        except Exception as ex:  # noqa: BLE001
            why = f"error {ex!r}"[:80]
        finally:
            shutil.rmtree(runner.RUN_DIR, ignore_errors=True)
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (c1.ru_utime - c0.ru_utime) + (c1.ru_stime - c0.ru_stime)
        if ok:
            p[2] = p[2] - {e, o}
            p[3].append(f"proxy: optimizer tpcds passed on {subset.count(',') + 1 if subset else 'all'} queries -> dropped exec tpcds ({cpu:.2f}s)")
        else:
            p[3].append(f"proxy: kept ({why}, {cpu:.2f}s)")


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
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tests = json.load(fh)["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    meta = {t["number"]: (t["file"], t["seconds"]) for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    tests_by_file = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], []).append(t["number"])
    mp = Map(MAP)
    TESTS_BY_FILE.update(tests_by_file)
    TEST_NUM.update(num)
    MAP_REF.append(mp)

    plans = []
    cdir = os.path.join(DATA, "commits")
    names = sorted(n for n in os.listdir(cdir) if n.endswith(".patch"))
    if os.environ.get("BR_SHARD"):  # offline batch tooling only: "i/n"
        si, sn = (int(x) for x in os.environ["BR_SHARD"].split("/"))
        names = names[si::sn]
    for name in names:
        with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        stem = name[:-6]
        files = parse_patch(text)
        try:
            sel, reasons = candidates(mp, files, tests_by_file, text, set(ms))
        except Exception as e:  # never crash: fall back to everything
            log(f"{stem}: selector error {e!r}; selecting all")
            sel, reasons = set(ms), ["error"]
        sel &= set(ms)
        plans.append([stem, text, sel, reasons, files])

    proxy_tpcds(plans, mp, num, ids, t_start, ms)

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

    for cid, _text, sel, reasons, _f in plans:
        cost, ws = pack(sel, ms)
        px = [r for r in reasons if r.startswith("proxy")]
        log(f"{cid}: {len(sel)} tests, {cost/1000:.1f}s {' | '.join(px)} {'; '.join(r for r in reasons if r not in px)[:300]}")
        print(json.dumps({"commit": cid, "workers": ws}), flush=True)


if __name__ == "__main__":
    main()
