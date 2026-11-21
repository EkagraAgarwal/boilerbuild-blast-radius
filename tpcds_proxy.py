"""Minimal, faithful replay of TestOptimizer::test_tpcds against a (patched) source tree.

python tpcds_proxy.py TREE  -> exit 0 if every TPC-DS query optimizes to the expected SQL,
exit 1 on the first mismatch or exception. Mirrors tests/test_optimizer.py check_file()
for "tpc-ds/tpc-ds" with optimizer.optimize, schema=TPCDS_SCHEMA, pretty=True, execute=False,
including parse_and_optimize's parent/arg_key assertions and the pickle round trip of the
result that the real test's process pool performs.

An optional third argument selects the suite: "tpcds" (default) or "tpch".
"""

import json
import pickle
import sys


def main():
    tree = sys.argv[1]
    only = {int(x) for x in sys.argv[2].split(",")} if len(sys.argv) > 2 and sys.argv[2] else None
    suite = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "tpcds"
    sys.path.insert(0, tree)
    from sqlglot import exp, parse_one
    from sqlglot.optimizer import optimizer
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs, string_to_bool

    if suite == "tpch":
        base_schema = TPCH_SCHEMA
        fixture = "optimizer/tpc-h/tpc-h.sql"
    else:
        base_schema = TPCDS_SCHEMA
        fixture = "optimizer/tpc-ds/tpc-ds.sql"

    cases = []
    for i, (meta, sql, expected) in enumerate(load_sql_fixture_pairs(fixture), start=1):
        if only is not None and (i - 1) not in only:
            continue
        cases.append((i, meta, sql, expected))
    cases.sort(key=lambda case: (len(case[2]), case[0]))

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
                    for child in arg if isinstance(arg, list) else [arg]:
                        if isinstance(child, exp.Expr):
                            assert child.arg_key == arg_key
                            assert child.parent is expression
            optimized = pickle.loads(pickle.dumps(optimized))
            actual = optimized.sql(pretty=True, dialect=dialect)
        except Exception as e:  # noqa: BLE001
            print(f"query {i}: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        if actual != expected:
            print(f"query {i}: mismatch", file=sys.stderr)
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
