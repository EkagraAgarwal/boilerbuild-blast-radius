"""Build-time precompute (runs in the Dockerfile against the pristine /sqlglot checkout).

Writes /app/index.marshal with, for every .py file under sqlglot/ and tests/:
  stmts:    [(lo, hi, kind, node_lo, node_hi, in_func), ...]  (same as selector.stmt_index)
  strlines: sorted line numbers that lie inside a multi-line string token
plus the coverage map converted from map.json.gz to plain marshal for fast loading.
Nothing here depends on patches: it is a map of the pinned commit.
"""

import ast
import gzip
import io
import json
import marshal
import os
import sys
import tokenize
from pathlib import Path


def stmts_of(text):
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out

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

    visit(tree, False)
    return out


def strlines_of(text):
    s = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)) and tok.end[0] > tok.start[0]:
                s.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return sorted(s)


FAMILY = ("dialects", "parsers", "generators", "tokenizers", "typing")


def dialect_index(src):
    """{"users": {dialect: [test ids naming it]}, "deps": {module rel: [dialects importing it]}}.

    A dialect module that fails to import breaks every test that names that dialect (or one that
    inherits from it), even tests whose coverage never reaches dialect-specific lines."""
    import ast

    names = sorted(p.stem for p in (src / "sqlglot" / "dialects").glob("*.py")
                   if p.stem not in ("__init__", "dialect"))
    nameset = set(names)

    def strings(node):
        return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    users = {}
    for p in sorted((src / "tests").rglob("test_*.py")):
        rel = p.relative_to(src).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                cls = set()
                for b in node.body:
                    if isinstance(b, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "dialect" for t in b.targets):
                        cls |= strings(b) & nameset
                for b in node.body:
                    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name.startswith("test"):
                        tid = f"{rel}::{node.name}::{b.name}"
                        for d in (strings(b) & nameset) | cls:
                            users.setdefault(d, []).append(tid)
                        # iterating the Dialects enum / Dialect.classes loads every dialect
                        if any(isinstance(n, ast.Name) and n.id == "Dialects" or
                               isinstance(n, ast.Attribute) and n.attr == "classes" for n in ast.walk(b)):
                            users.setdefault("*", []).append(tid)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                for d in strings(node) & nameset:
                    users.setdefault(d, []).append(f"{rel}::{node.name}")

    # top-level imports between dialect-family modules
    mods = {}
    for sub in FAMILY:
        for p in (src / "sqlglot" / sub).glob("*.py"):
            rel = p.relative_to(src).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            deps = set()
            for node in tree.body:
                targets = []
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot."):
                    targets.append(node.module)
                    targets += [f"{node.module}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Import):
                    targets += [a.name for a in node.names if a.name.startswith("sqlglot.")]
                for t in targets:
                    parts = t.split(".")
                    if len(parts) >= 3 and parts[1] in FAMILY:
                        deps.add(f"sqlglot/{parts[1]}/{parts[2]}.py")
            mods[rel] = deps
    needed_by = {}
    for d in names:
        root = f"sqlglot/dialects/{d}.py"
        seen, stack = set(), [root]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(mods.get(x, ()))
        for x in seen:
            needed_by.setdefault(x, []).append(d)
    # fixture files that name dialects (e.g. "# dialect: spark" metadata): tests opening them use them
    import re
    fix = {}
    word = re.compile(r"[a-z][a-z0-9]+")
    for p in sorted((src / "tests" / "fixtures").rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        found = sorted(set(word.findall(text)) & nameset)
        if found:
            fix[p.relative_to(src).as_posix()] = found
    return {"users": users, "deps": {k: sorted(v) for k, v in needed_by.items()}, "fixtures": fix}


def import_index(src):
    """{"eager": [modules `import sqlglot` loads], "testmods": {test file: [sqlglot modules its import
    loads]}, "importers": {module: [modules whose top-level import (transitively) needs it]}}.

    Used when a patch can make a module fail to import (syntax error): exactly the tests that load it
    break. Module paths are repo-relative ("sqlglot/x/y.py")."""
    import ast
    import subprocess

    probe = (
        "import sys, json, importlib\n"
        "base = set(sys.modules)\n"
        "target = sys.argv[1]\n"
        "try:\n"
        "    importlib.import_module(target)\n"
        "except BaseException:\n"
        "    pass\n"
        "out = sorted({m.__file__ for n, m in list(sys.modules.items())\n"
        "              if getattr(m, '__file__', None) and n.split('.')[0] == 'sqlglot'})\n"
        "print(json.dumps(out))\n")
    env = dict(os.environ, PYTHONPATH=str(src), PYTHONDONTWRITEBYTECODE="1")

    def loaded(modname):
        try:
            r = subprocess.run([sys.executable, "-c", probe, modname], cwd=str(src), env=env,
                               capture_output=True, text=True, timeout=120)
            files = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            return None
        rels = []
        for f in files:
            p = Path(f).resolve()
            try:
                rels.append(p.relative_to(src.resolve()).as_posix())
            except ValueError:
                pass
        return rels

    eager = loaded("sqlglot") or []
    testmods = {}
    for p in sorted((src / "tests").rglob("test_*.py")):
        rel = p.relative_to(src).as_posix()
        mods = loaded(rel[:-3].replace("/", "."))
        if mods is not None:
            testmods[rel] = [m for m in mods if m not in eager]
    # top-level import graph inside sqlglot/
    deps = {}
    lazy = {}
    for p in (src / "sqlglot").rglob("*.py"):
        rel = p.relative_to(src).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        ds = set()
        for node in tree.body:
            names = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot"):
                names.append(node.module)
                names += [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names += [a.name for a in node.names if a.name.startswith("sqlglot")]
            for n in names:
                for cand in (n.replace(".", "/") + ".py", n.replace(".", "/") + "/__init__.py"):
                    if (src / cand).exists():
                        ds.add(cand)
        deps[rel] = ds
        # imports anywhere (incl. inside functions): their lines' runtime users load the module
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot"):
                names.append(node.module)
                names += [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names += [a.name for a in node.names if a.name.startswith("sqlglot")]
            for n in names:
                for cand in (n.replace(".", "/") + ".py", n.replace(".", "/") + "/__init__.py"):
                    if (src / cand).exists():
                        lazy.setdefault(cand, []).append([rel, node.lineno])
    importers = {}
    rev = {}
    for m, ds in deps.items():
        for d in ds:
            rev.setdefault(d, set()).add(m)
    for mod in deps:
        seen, stack = set(), [mod]
        while stack:
            x = stack.pop()
            for y in rev.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        importers[mod] = sorted(seen)
    return {"eager": eager, "testmods": testmods, "importers": importers, "lazy": lazy}


def main():
    src, out, mapgz = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    import re
    files = {}
    texts = {}
    names = {}
    ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
    for sub in ("sqlglot", "tests"):
        for p in sorted((src / sub).rglob("*.py")):
            rel = p.relative_to(src).as_posix()
            text = p.read_text(encoding="utf-8")
            texts[rel] = text.encode("utf-8")
            # each file's entry is its own marshal blob: decoded only if a patch touches it
            files[rel] = marshal.dumps({"stmts": stmts_of(text), "strlines": strlines_of(text)})
            for w in set(ident.findall(text)):
                names.setdefault(w, []).append(rel)
    m = json.loads(gzip.decompress(mapgz.read_bytes()))
    dialects = marshal.dumps(dialect_index(src))
    imports = marshal.dumps(import_index(src))
    with open(out, "wb") as f:
        marshal.dump({"files": files, "map": marshal.dumps(m), "names": marshal.dumps(names), "texts": texts,
                      "dialects": dialects, "imports": imports}, f)
    print(f"index: {len(files)} files -> {out.stat().st_size / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
