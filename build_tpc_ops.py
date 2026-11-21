"""Build a compact JSON inventory of the SQL operations exercised by the
TPC-DS and TPC-H optimizer fixtures.

Runs once, at Docker build time, against a pristine sqlglot checkout:

    python build_tpc_ops.py <sqlglot_src_root> <out_json_path>

The result is deliberately over-approximated (no false omissions): a mutant is
only relevant if it touches an expression class, PythonGenerator method, ENV
key or SQL token that these queries can actually reach.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

FIXTURES = (
    ("tpcds", "optimizer/tpc-ds/tpc-ds.sql"),
    ("tpch", "optimizer/tpc-h/tpc-h.sql"),
)


def _expr_mro_names(node, out):
    """Record the name of every exp.Expr class in a node's MRO."""
    for cls in type(node).__mro__:
        if isinstance(cls, type) and issubclass(cls, out["Expr"]):
            out["exprs"].add(cls.__name__)


def _iter_exprs(obj, expr_type):
    """Yield every exp.Expr reachable through a plan-step attribute."""
    if isinstance(obj, expr_type):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_exprs(key, expr_type)
            yield from _iter_exprs(value, expr_type)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            yield from _iter_exprs(item, expr_type)


def main():
    src_root = sys.argv[1]
    out_path = sys.argv[2]

    sys.path.insert(0, src_root)

    # Imports must happen only after the pristine source root is on sys.path.
    from sqlglot import exp, parse_one
    from sqlglot.executor.env import ENV
    from sqlglot.executor.python import PythonExecutor  # noqa: F401  (import contract)
    from sqlglot.generators.python import PythonGenerator
    from sqlglot.planner import Plan
    from tests.helpers import load_sql_fixture_pairs

    all_env_keys = set(ENV.keys())
    env_by_lower = {key.lower(): key for key in all_env_keys}

    # ENV keys that the executor references statically (as string literals).
    static_env_keys = set()
    try:
        with open(
            os.path.join(src_root, "sqlglot", "executor", "python.py"), encoding="utf-8"
        ) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in all_env_keys:
                    static_env_keys.add(node.value)
    except Exception:
        pass

    ctx = {"Expr": exp.Expr, "exprs": None}

    class _TracingPythonGenerator(PythonGenerator):
        """Records the generated method names and expression classes touched."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.recorded_methods = set()

        def sql(self, expression, key=None, comment=True):
            if isinstance(expression, exp.Expr) and not key:
                self.recorded_methods.add(f"{expression.key}_sql")
            return super().sql(expression, key=key, comment=comment)

    result = {}

    for suite, fixture in FIXTURES:
        fixture_path = os.path.join(src_root, "tests", "fixtures", fixture)
        with open(fixture_path, encoding="utf-8") as fh:
            raw_text = fh.read()

        tokens = {token.lower() for token in TOKEN_RE.findall(raw_text)}

        exprs = set()
        env_keys = set()
        methods = set()
        code_strings = []
        ctx["exprs"] = exprs

        for meta, sql, expected in load_sql_fixture_pairs(fixture):
            dialect = meta.get("dialect")
            roots = []
            for text in (sql, expected):
                try:
                    parsed = parse_one(text, read=dialect)
                except Exception:
                    parsed = None
                if parsed is not None:
                    roots.append(parsed)

            # Expression classes from the input ASTs and the expected ASTs.
            for root in roots:
                for node in root.walk():
                    _expr_mro_names(node, ctx)

            gen = _TracingPythonGenerator()

            def emit(node):
                if not isinstance(node, exp.Expr):
                    return
                _expr_mro_names(node, ctx)
                # PythonGenerator.sql() dispatches every expression through
                # sql(), so this both produces the Python source and traces
                # the generator method / ENV function used for that node.
                try:
                    generated = gen.sql(node)
                except Exception:
                    return
                if isinstance(generated, str):
                    code_strings.append(generated)

            # Generate Python directly for both root expressions.
            for root in roots:
                emit(root)

            # Generate Python for every top-level expression in every plan
            # step. The plan is derived from the input query; the optimizer's
            # expected query is covered by the root generation above.
            for root in roots[:1]:
                try:
                    plan = Plan(root)
                    steps = list(plan.dag)
                except Exception:
                    continue
                for step in steps:
                    for attribute in vars(step).values():
                        for node in _iter_exprs(attribute, exp.Expr):
                            emit(node)

            methods.update(gen.recorded_methods)

        # ENV keys referenced by the generated Python source.
        for code in code_strings:
            for token in TOKEN_RE.findall(code):
                if token in all_env_keys:
                    env_keys.add(token)
                upper = token.upper()
                if upper in all_env_keys:
                    env_keys.add(upper)

        env_keys.update(static_env_keys)

        # Zero-false-negative guards: expression names and SQL tokens that
        # coincide (case-insensitively) with an ENV key.
        for name in exprs:
            key = env_by_lower.get(name.lower())
            if key is not None:
                env_keys.add(key)
        for token in tokens:
            key = env_by_lower.get(token)
            if key is not None:
                env_keys.add(key)

        result[suite] = {
            "exprs": sorted(exprs),
            "env_keys": sorted(env_keys),
            "pygen_methods": sorted(methods),
            "tokens": sorted(tokens),
        }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
