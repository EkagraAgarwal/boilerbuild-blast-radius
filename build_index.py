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
    with open(out, "wb") as f:
        marshal.dump({"files": files, "map": marshal.dumps(m), "names": marshal.dumps(names), "texts": texts}, f)
    print(f"index: {len(files)} files -> {out.stat().st_size / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
