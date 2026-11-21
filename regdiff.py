"""Standalone runtime registry diff prototype (slot 3 / Agent 3).

Pure-Python, dependency-free helpers for deciding whether an import-time
"registry" mutation in sqlglot can possibly influence the TPC-DS / TPC-H
queries.  Everything here uses only the standard library (``ast``, ``re``,
``os``, ``time``) -- no subprocesses, no import of sqlglot, no third-party
packages.

Public API
----------
``tpcds_static_uses(src_root="/sqlglot")``
    Statically collect the function/identifier tokens used by the 99 TPC-DS
    and 22 TPC-H queries (both the input and the expected optimized SQL are
    in the fixtures).  Cached per source root; a cold scan is a few tens of
    milliseconds (well within the 0.15 s budget) and repeat calls are O(1).

``diff_class_dict_ast(old_src, new_src)``
    Compare pristine vs patched module source with :mod:`ast` only and report
    module/class level dict, set and list assignments whose keys/items changed.
    Handles ``{**Parent.DICT, ...}`` spreads, ``exp.X`` keys/values, string
    keys, ``_sql_names`` lists and added ``*_sql`` generator methods.

``check_tpc_uses_expr_or_env(src_root, expr_names, env_keys)``
    Answer whether any of the given expression names / ENV dict keys could be
    exercised by the TPC fixtures.

The ``__main__`` section runs the prototype against the six reference patches
and prints a short benchmark summary.
"""

from __future__ import annotations

import ast
import os
import re
import time
from collections import OrderedDict

__all__ = [
    "tpcds_static_uses",
    "diff_class_dict_ast",
    "check_tpc_uses_expr_or_env",
]

# --------------------------------------------------------------------------- #
# source-root resolution
# --------------------------------------------------------------------------- #

def _resolve_src_root(src_root=None):
    """Resolve ``src_root`` to a directory containing ``sqlglot/`` + ``tests/``.

    The scorer passes ``/sqlglot`` (BR_SRC in the Docker image).  For local
    testing the checkout lives next to ``submission/`` so we fall back to a
    sibling ``../sqlglot`` and to the ``BR_SRC`` environment variable.
    """
    candidates = []
    if src_root:
        candidates.append(src_root)
    env = os.environ.get("BR_SRC")
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "sqlglot"))
    candidates.append(os.path.join(here, "..", "sqlglot"))
    candidates.append("/sqlglot")
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isdir(os.path.join(path, "sqlglot")) and os.path.isdir(
            os.path.join(path, "tests")
        ):
            return path
    # Nothing exists: return the first candidate so callers still get a
    # deterministic (empty) result rather than an exception.
    return os.path.abspath(candidates[0]) if candidates else "/sqlglot"


# --------------------------------------------------------------------------- #
# static TPC-DS / TPC-H token index
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TPC_CACHE = {}


def _normalize(name):
    """Case/underscore-insensitive form used to compare a name with SQL text.

    ``ToChar``, ``tochar_sql``, ``TO_CHAR`` and ``TOCHAR`` all normalize to
    ``tochar`` so CamelCase / snake_case spelling differences cannot create a
    false miss.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def tpcds_static_uses(src_root="/sqlglot"):
    """Return the set of identifiers/function tokens used by the TPC fixtures.

    The result is a dict with two per-file token sets (raw + normalized),
    their union, and the query counts.  It is cached per resolved root, so
    repeated calls are O(1).
    """
    root = _resolve_src_root(src_root)
    if root in _TPC_CACHE:
        return _TPC_CACHE[root]

    paths = {
        "tpc_ds": os.path.join(
            root, "tests", "fixtures", "optimizer", "tpc-ds", "tpc-ds.sql"
        ),
        "tpc_h": os.path.join(
            root, "tests", "fixtures", "optimizer", "tpc-h", "tpc-h.sql"
        ),
    }
    result = {"src_root": root}
    normalized = {}
    raw_tokens = {}
    for label, path in paths.items():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        toks = frozenset(_TOKEN_RE.findall(text))
        norm = frozenset(_normalize(t) for t in toks)
        result[label + "_tokens"] = toks
        result[label + "_norm"] = norm
        normalized[label] = norm
        raw_tokens[label] = toks
        # TPC-DS headers look like "-- TPC-DS 1"; TPC-H like "-- TPC-H 1".
        result[label + "_queries"] = len(
            re.findall(r"^--\s*TPC-{}S?\s+\d+".format("D" if label == "tpc_ds" else "H"),
                       text, re.M)
        ) or text.count("# execute:")

    result["all_norm"] = normalized["tpc_ds"] | normalized["tpc_h"]
    result["all_tokens"] = raw_tokens["tpc_ds"] | raw_tokens["tpc_h"]
    _TPC_CACHE[root] = result
    return result


_EXPR_CLASS_CACHE = {}


def _expr_class_map(root):
    """Map normalized class name -> set of expression classes in sqlglot.

    Used to resolve an ENV key such as ``DATESTRTODATE`` to ``DateStrToDate``
    without importing sqlglot.  Scans ``sqlglot/expressions/*.py`` once.
    """
    if root in _EXPR_CLASS_CACHE:
        return _EXPR_CLASS_CACHE[root]
    mapping = {}
    expr_dir = os.path.join(root, "sqlglot", "expressions")
    if os.path.isdir(expr_dir):
        pattern = re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M)
        for name in sorted(os.listdir(expr_dir)):
            if not name.endswith(".py"):
                continue
            try:
                with open(os.path.join(expr_dir, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for cls in pattern.findall(text):
                mapping.setdefault(_normalize(cls), set()).add(cls)
    _EXPR_CLASS_CACHE[root] = mapping
    return mapping


def check_tpc_uses_expr_or_env(src_root, expr_names=(), env_keys=()):
    """Determine whether the TPC fixtures could invoke any given name/key.

    Returns a dict with per-name hit information plus convenience lists of the
    names that were actually used.  ``src_root`` is resolved exactly as in
    :func:`tpcds_static_uses`.
    """
    uses = tpcds_static_uses(src_root)
    ds_norm = uses["tpc_ds_norm"]
    ch_norm = uses["tpc_h_norm"]
    class_map = _expr_class_map(uses["src_root"])

    def info(name):
        candidates = [name]
        candidates.extend(sorted(class_map.get(_normalize(name), ())))
        in_ds = any(_normalize(c) in ds_norm for c in candidates)
        in_ch = any(_normalize(c) in ch_norm for c in candidates)
        return {
            "tpcds": bool(in_ds),
            "tpc_h": bool(in_ch),
            "any": bool(in_ds or in_ch),
            "candidates": candidates,
        }

    expr = {name: info(name) for name in expr_names}
    env = {key: info(key) for key in env_keys}
    all_hits = list(expr.values()) + list(env.values())
    return {
        "expr": expr,
        "env": env,
        "used_exprs": [n for n, v in expr.items() if v["any"]],
        "used_env_keys": [k for k, v in env.items() if v["any"]],
        "any": any(v["any"] for v in all_hits),
    }


# --------------------------------------------------------------------------- #
# AST helpers for diff_class_dict_ast
# --------------------------------------------------------------------------- #

def _dotted(node):
    """Return the dotted name for an Attribute/Name chain, or ''."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _expr_names(node):
    """Expression class names referenced by ``node`` (``exp.X`` -> ``'X'``)."""
    out = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Attribute):
            continue
        dotted = _dotted(sub)
        for prefix in ("exp.", "sqlglot.exp.", "E."):
            if dotted.startswith(prefix):
                out.add(dotted[len(prefix):].split(".")[0])
                break
    return out


def _str_keys(node):
    """String constants found directly in ``node`` (used for dict keys)."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def _key_id(node):
    """Stable, hashable identity for a dict key / collection item node."""
    if isinstance(node, ast.Constant):
        return ("c", type(node.value).__name__, str(node.value))
    return ("a", ast.dump(node))


def _key_label(node):
    """Human-readable label for a key/item."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    dotted = _dotted(node)
    if dotted:
        return dotted
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node)


def _dict_map(dict_node):
    """Map key-identity -> (key_node, value_node), skipping ``**spread`` entries."""
    out = OrderedDict()
    for key, value in zip(dict_node.keys, dict_node.values):
        if key is None:  # **Parent.DICT
            continue
        out[_key_id(key)] = (key, value)
    return out


def _expr_from_method(method_name):
    """Derive an expression-name candidate from a generator transform method.

    ``tochar_sql`` -> ``Tochar`` (the TPC check normalizes to ``tochar`` and
    therefore also catches ``TO_CHAR`` / ``TOCHAR`` spellings).
    """
    base = method_name
    if base.endswith("_sql"):
        base = base[:-4]
    base = base.lstrip("_")
    parts = [p for p in base.split("_") if p]
    if not parts:
        return set()
    return {"".join(p[:1].upper() + p[1:] for p in parts)}


def _collect_slots(src):
    """Collect module/class level assignments and methods from ``src``.

    A name can be assigned more than once in the same scope (e.g. sqlglot's
    ``FUNCTIONS`` dict is declared and then filtered); every occurrence is kept
    in source order so a diff can be paired up correctly.  Each entry is
    ``(kind, node, source_fingerprint)``; the fingerprint lets unchanged slots
    be skipped without an expensive :func:`ast.dump` comparison.
    """
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    slots = OrderedDict()

    def fingerprint(node):
        try:
            start = offsets[node.lineno - 1] + node.col_offset
            end = offsets[node.end_lineno - 1] + node.end_col_offset
        except (AttributeError, IndexError, TypeError):
            return None
        return " ".join(src[start:end].split())

    def add(key, kind, value):
        slots.setdefault(key, []).append((kind, value, fingerprint(value)))

    def add_assign(cls, node):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for target in targets:
            if isinstance(target, ast.Name):
                key = (cls + "." + target.id) if cls else target.id
                add(key, "assign", value)

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            add_assign("", node)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    add_assign(node.name, sub)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(node.name + "." + sub.name, "method", sub)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, "method", node)
    return slots


def _method_record(scope, kind, method_names):
    exprs = set()
    for name in method_names:
        exprs |= _expr_from_method(name)
    return {
        "scope": scope,
        "kind": kind,
        "changed_keys": list(method_names) if kind == "method_changed" else [],
        "added_keys": list(method_names) if kind == "method_added" else [],
        "removed_keys": list(method_names) if kind == "method_removed" else [],
        "expr_names": sorted(exprs),
        "str_keys": [],
    }


def _describe_changed(scope, old_slot, new_slot):
    # Fast path: identical source text means the AST cannot differ.  This skips
    # an expensive ast.dump for the vast majority of unchanged slots.
    old_fp, new_fp = old_slot[2], new_slot[2]
    if old_fp is not None and new_fp is not None and old_fp == new_fp:
        return None

    if old_slot[0] != "assign" or new_slot[0] != "assign":
        if old_slot[0] == "method" and new_slot[0] == "method":
            if ast.dump(old_slot[1]) == ast.dump(new_slot[1]):
                return None
            return _method_record(scope, "method_changed", [scope.split(".")[-1]])
        return None

    old_val, new_val = old_slot[1], new_slot[1]
    if ast.dump(old_val) == ast.dump(new_val):
        return None

    kind = "value"
    changed, added, removed = [], [], []
    affected = []  # (key_node_or_None, value_node_or_None)

    if isinstance(old_val, ast.Dict) and isinstance(new_val, ast.Dict):
        kind = "dict_keys"
        om, nm = _dict_map(old_val), _dict_map(new_val)
        for k_id, (key, value) in om.items():
            if k_id not in nm:
                removed.append(_key_label(key))
                affected.append((key, value))
        for k_id, (key, value) in nm.items():
            if k_id not in om:
                added.append(_key_label(key))
                affected.append((key, value))
        for k_id, (key, old_v) in om.items():
            if k_id in nm:
                new_v = nm[k_id][1]
                if ast.dump(old_v) != ast.dump(new_v):
                    changed.append(_key_label(key))
                    affected.append((key, new_v))

    elif isinstance(old_val, (ast.List, ast.Set, ast.Tuple)) and isinstance(
        new_val, (ast.List, ast.Set, ast.Tuple)
    ):
        kind = "list_items"
        oi = {_key_id(i): i for i in old_val.elts}
        ni = {_key_id(i): i for i in new_val.elts}
        for k_id, item in oi.items():
            if k_id not in ni:
                removed.append(_key_label(item))
                affected.append((item, None))
        for k_id, item in ni.items():
            if k_id not in oi:
                added.append(_key_label(item))
                affected.append((item, None))
    else:
        changed = ["<value>"]
        affected.append((old_val, new_val))

    exprs, strs = set(), set()
    for key, value in affected:
        if key is not None:
            exprs |= _expr_names(key)
            strs |= _str_keys(key)
        if value is not None:
            exprs |= _expr_names(value)

    # For a class attribute (e.g. Getbit._sql_names) the owning class is also
    # a useful expression-name candidate.
    if "." in scope:
        owner = scope.split(".")[0]
        if owner[:1].isupper():
            exprs.add(owner)

    return {
        "scope": scope,
        "kind": kind,
        "changed_keys": sorted(changed),
        "added_keys": sorted(added),
        "removed_keys": sorted(removed),
        "expr_names": sorted(exprs),
        "str_keys": sorted(strs),
    }


def _describe_slot(scope, slot, kind):
    """Describe a slot that exists in only one of the two sources."""
    if slot[0] == "method":
        return _method_record(
            scope,
            "method_added" if kind == "slot_added" else "method_removed",
            [scope.split(".")[-1]],
        )
    value = slot[1]
    if isinstance(value, ast.Dict):
        keys = [_key_label(k) for k in value.keys if k is not None]
        kind_name = "dict_keys"
    elif isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        keys = [_key_label(i) for i in value.elts]
        kind_name = "list_items"
    else:
        keys = ["<value>"]
        kind_name = "value"
    return {
        "scope": scope,
        "kind": kind_name,
        "changed_keys": [],
        "added_keys": keys if kind == "slot_added" else [],
        "removed_keys": keys if kind == "slot_removed" else [],
        "expr_names": sorted(_expr_names(value)),
        "str_keys": sorted(_str_keys(value)),
    }


def diff_class_dict_ast(old_src, new_src):
    """Diff module/class-level registry assignments using only :mod:`ast`.

    Parameters
    ----------
    old_src, new_src:
        Full Python source text of the pristine and patched module.

    Returns
    -------
    list of dict
        One record per changed slot::

            {
              "scope": "ClassName.ATTR" | "ATTR",
              "kind": "dict_keys" | "list_items" | "value"
                      | "method_added" | "method_changed",
              "changed_keys": [...], "added_keys": [...],
              "removed_keys": [...], "expr_names": [...],
              "str_keys": [...],
            }

    ``expr_names`` holds expression classes (``exp.ToChar`` -> ``'ToChar'``)
    and ``str_keys`` holds literal keys (``'DATESTRTODATE'``, ``'ARRAY_AGG'``).
    ``{**Parent.DICT, ...}`` spreads are ignored as unchanged context.
    """
    old_slots = _collect_slots(old_src)
    new_slots = _collect_slots(new_src)

    records = []
    for scope in list(old_slots) + [s for s in new_slots if s not in old_slots]:
        old_entries = old_slots.get(scope, [])
        new_entries = new_slots.get(scope, [])
        if not old_entries:
            for entry in new_entries:
                records.append(_describe_slot(scope, entry, "slot_added"))
        elif not new_entries:
            for entry in old_entries:
                records.append(_describe_slot(scope, entry, "slot_removed"))
        else:
            for index in range(max(len(old_entries), len(new_entries))):
                old_entry = old_entries[index] if index < len(old_entries) else None
                new_entry = new_entries[index] if index < len(new_entries) else None
                if old_entry is None:
                    records.append(_describe_slot(scope, new_entry, "slot_added"))
                elif new_entry is None:
                    records.append(_describe_slot(scope, old_entry, "slot_removed"))
                else:
                    rec = _describe_changed(scope, old_entry, new_entry)
                    if rec is not None:
                        records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# patch helpers (used by the self-test in __main__; no external dependency)
# --------------------------------------------------------------------------- #

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_file_source(pristine_src, patch_text, rel_path):
    """Apply the hunks for ``rel_path`` from a unified diff to pristine source."""
    old = pristine_src.replace("\r\n", "\n").split("\n")
    lines = patch_text.replace("\r\n", "\n").split("\n")
    out = []
    pos = 0
    in_file = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            if path.startswith("b/"):
                path = path[2:]
            in_file = path == rel_path
            i += 1
            continue
        if in_file:
            match = _HUNK_RE.match(line)
            if match:
                start = int(match.group(1)) - 1
                if (match.group(2) or "1") == "0":
                    start = int(match.group(1))
                start = max(0, min(start, len(old)))
                out.extend(old[pos:start])
                pos = start
                i += 1
                while i < len(lines):
                    cur = lines[i]
                    if cur.startswith(" ") or cur == "":
                        if cur == "":
                            nxt = lines[i + 1][:1] if i + 1 < len(lines) else ""
                            if nxt not in (" ", "-", "+"):
                                break
                            out.append("")
                            pos += 1
                        else:
                            out.append(cur[1:])
                            pos += 1
                    elif cur.startswith("-") and not cur.startswith("---"):
                        pos += 1
                    elif cur.startswith("+") and not cur.startswith("+++"):
                        out.append(cur[1:])
                    elif cur.startswith("\\"):
                        pass
                    else:
                        break
                    i += 1
                continue
            if line.startswith("diff --git"):
                in_file = False
        i += 1
    out.extend(old[pos:])
    return "\n".join(out)


def _patched_python_files(patch_text):
    """Return the ``b/...`` .py paths touched by a patch."""
    paths = []
    for line in patch_text.replace("\r\n", "\n").split("\n"):
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            if path.startswith("b/") and path.endswith(".py"):
                paths.append(path[2:])
    return paths


# --------------------------------------------------------------------------- #
# self-test / benchmark
# --------------------------------------------------------------------------- #

_PATCHES = [
    (r"mutl\commits\m31049.patch", "ENV key rename"),
    (r"mutl\commits\m31065.patch", "added exp.ToChar generator method"),
    (r"mutl\commits\m31030.patch", "MySQL FUNCTIONS key rename"),
    (r"mutl2\commits\m41006.patch", "ClickHouse TRANSFORMS value edit"),
    (r"mutl2\commits\m41010.patch", "Getbit._sql_names edit"),
    (r"newkinds\commits\k021.patch", "Snowflake FUNCTIONS key rename"),
]


def _main():
    here = os.path.dirname(os.path.abspath(__file__))
    work = os.path.join(os.path.dirname(here), "work")
    src_root = _resolve_src_root()

    t0 = time.perf_counter()
    uses = tpcds_static_uses(src_root)
    scan_ms = (time.perf_counter() - t0) * 1000.0
    print("TPC static index: {} TPC-DS queries, {} TPC-H queries, {} "
          "normalized tokens ({:.2f} ms)".format(
              uses["tpc_ds_queries"], uses["tpc_h_queries"],
              len(uses["all_norm"]), scan_ms))

    print("\n{:<10} {:<34} {:<12} {:<14} {:<6} {:>7} {}".format(
        "patch", "file", "scope", "kind", "TPC?", "ms", "detected keys / names"))
    print("-" * 118)

    total = 0.0
    for rel_patch, label in _PATCHES:
        patch_path = os.path.join(work, rel_patch)
        try:
            with open(patch_path, encoding="utf-8") as fh:
                patch_text = fh.read()
        except OSError as exc:
            print("{:<10} MISSING PATCH ({})".format(os.path.basename(rel_patch), exc))
            continue

        expr_names, str_keys = set(), set()
        patch_ms = 0.0
        for rel in _patched_python_files(patch_text):
            pristine_path = os.path.join(src_root, *rel.split("/"))
            try:
                with open(pristine_path, encoding="utf-8") as fh:
                    pristine = fh.read()
            except OSError:
                continue
            patched = _patch_file_source(pristine, patch_text, rel)
            t0 = time.perf_counter()
            records = diff_class_dict_ast(pristine, patched)
            patch_ms += (time.perf_counter() - t0) * 1000.0
            for rec in records:
                expr_names.update(rec["expr_names"])
                str_keys.update(rec["str_keys"])
                hit = check_tpc_uses_expr_or_env(src_root, rec["expr_names"], rec["str_keys"])
                changes = []
                for field in ("removed_keys", "added_keys", "changed_keys"):
                    if rec[field]:
                        changes.append("{}[{}]".format(field[0], ", ".join(rec[field])))
                detail = " ".join(changes)
                if rec["expr_names"]:
                    detail += " exp={{{}}}".format(", ".join(rec["expr_names"]))
                print("{:<10} {:<34} {:<12} {:<14} {:<6} {:>7.2f} {}".format(
                    os.path.basename(rel_patch).split(".")[0],
                    rel, rec["scope"], rec["kind"],
                    "YES" if hit["any"] else "NO", patch_ms, detail))
        total += patch_ms
        # Ensure ENV/expression keys are also checked as env keys, not just strs.
        check_tpc_uses_expr_or_env(src_root, expr_names, str_keys)

    print("-" * 118)
    print("Total detection time for 6 patches: {:.2f} ms".format(total))


if __name__ == "__main__":
    _main()
