"""Label single-line source mutants by running only a candidate list of tests each.

Runs inside the br-env Linux container. Each worker keeps one private /src copy,
mutates/restores one line per mutant; the parent appends one JSON line per mutant.
"""
import argparse, glob, json, multiprocessing as mp, os, shutil, signal, subprocess, sys, time

SRC, TOOLS, WORK, TESTS = "/src", "/tools", "/work/allmut", "/work/tests.json"
BY_ID, BY_NUM, BY_FILE, WORK_DIR, FAIL_SEQ = {}, {}, {}, "", 0
EXEC, OPT = 1106, 1440  # test_execute_tpcds, TestOptimizer::test_tpcds
UNSAFE = ("sqlglot/executor/", "sqlglot/generator.py", "sqlglot/generators/", "sqlglot/serde.py",
          "sqlglot/dialects/duckdb.py", "sqlglot/dialects/python.py")
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")


def init_worker():
    """One private, pristine copy of /src per worker process."""
    global WORK_DIR
    tmp = f"/tmp/lt_{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    WORK_DIR = os.path.join(tmp, "src")
    shutil.copytree(SRC, WORK_DIR, ignore=IGNORE)
    # byte-compile once: each run then only recompiles the one mutated file (mtime check)
    subprocess.run([sys.executable, "-m", "compileall", "-q", os.path.join(WORK_DIR, "sqlglot"),
                    os.path.join(WORK_DIR, "tests")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def result(mid, cand_ms, status, failing, outside, secs, msg):
    if ONLY:
        return {"id": mid, "status": status, "only": sorted(ONLY), "failing": sorted(failing), "secs": round(secs, 3),
                "msg": msg}
    return {"id": mid, "status": status, "failing": sorted(failing),
            "outside": sorted(outside), "cand_ms": cand_ms,
            "secs": round(secs, 3), "msg": msg}


def read_label(out):
    failed, collect = set(), set()
    for fn in ([out] if os.path.exists(out) else sorted(glob.glob(out + "*"))):
        try:
            with open(fn) as f:
                d = json.load(f)
            failed |= set(d.get("failed", ()))
            collect |= set(d.get("collect_errors", ()))
        except Exception:
            pass
    return failed, collect


def map_numbers(failed, collect):
    """Mirror mutate.py label(): exact id, else prefix; collect errors map whole file."""
    nums = set()
    for nid in failed:
        if nid in BY_ID:
            nums.add(BY_ID[nid])
        else:
            nums |= {v for k, v in BY_ID.items() if k.startswith(nid)}
    for c in collect:
        nums |= set(BY_FILE.get(c.split("::")[0], ()))
    return nums


def collapse_if_long(nodeids, cand):
    """For a huge command, use a file path for files whose tests are all candidates."""
    if len(" ".join(nodeids)) <= 500000:
        return nodeids
    by_file = {}
    for n in cand:
        if n in BY_NUM:
            by_file.setdefault(BY_NUM[n]["file"], []).append(n)
    out = []
    for f, nums in by_file.items():
        if set(BY_FILE[f]) <= cand:
            out.append(f)
        else:
            out.extend(BY_NUM[n]["id"] for n in nums)
    return out


PROXY = os.environ.get("LT_PROXY", "/sub/tpcds_proxy.py")
SLACK = float(os.environ.get("LT_SLACK", "1"))
# LT_ONLY="1106": re-run just these tests (e.g. to check an inferred executor TPC-DS failure)
ONLY = {int(x) for x in os.environ.get("LT_ONLY", "").split(",") if x.strip()}


_QM = []


def proxy_subset(rel, lineno, lines):
    """TPC-DS queries whose coverage touches the function enclosing the mutated line ("" = all 99).
    Same query map the selector's proxy uses; a line outside any function replays everything."""
    try:
        import ast
        sys.path.insert(0, os.path.dirname(PROXY))
        import partial
        if not _QM:
            import gzip
            _QM.append(json.loads(gzip.decompress(open(os.path.join(os.path.dirname(PROXY), "querymap.json.gz"),
                                                         "rb").read())))
        tree = ast.parse(chr(10).join(lines))
        best = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= lineno <= node.end_lineno:
                if best is None or node.lineno > best.lineno:
                    best = node
        if best is None:
            return ""
        lo = min([best.lineno] + [d.lineno for d in best.decorator_list])
        q, setup = partial.queries_touching(_QM[0], "tpcds", {rel: set(range(lo, best.end_lineno + 1))})
        if setup or not q:
            return ""
        return ",".join(map(str, q))
    except Exception:  # noqa: BLE001
        return ""


def collect_of(out):
    return read_label(out)[1]


MEM_MB = int(os.environ.get("LT_MEM_MB", "0"))


def _limit_mem():
    """Cap each process's address space so one runaway mutant can't take the whole runner down."""
    if MEM_MB:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEM_MB << 20, MEM_MB << 20))


def run_group(cmd, env, limit):
    """Run cmd in its own session; kill the group on timeout. -> (returncode, 'ok'|'timeout')."""
    proc = subprocess.Popen(cmd, cwd=WORK_DIR, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=_limit_mem)
    try:
        proc.wait(timeout=limit)
        return proc.returncode, "ok"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return None, "timeout"


def run_one(task):
    mid, rel, lineno, old, new, op, nodeids, cand, cand_ms = task
    t0 = time.time()
    global FAIL_SEQ
    FAIL_SEQ += 1
    path = os.path.join(WORK_DIR, rel)
    out = os.path.join(WORK_DIR, f".brlabel_{os.getpid()}_{FAIL_SEQ}.json")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()
        lines = original.split("\n")
        idx = lineno - 1
        if idx < 0 or idx >= len(lines) or lines[idx] != old:
            return result(mid, cand_ms, "mismatch", [], [], time.time() - t0, "line mismatch")
    except Exception as e:
        return result(mid, cand_ms, "error", [], [], time.time() - t0, repr(e)[:200])

    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines[:idx] + [new] + lines[idx + 1:]))
        env = dict(os.environ, PYTHONPATH=f"{WORK_DIR}:{TOOLS}", BRLABEL_OUT=out,
                   SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2")
        limit = max(240, 6 * (cand_ms / 1000.0) + 120) * SLACK
        msg = ""
        # optimizer TPC-DS via the faithful single-process replay (all 99 queries); the executor's
        # TPC-DS outside executor/generator code only fails together with it
        skip = set()
        proxy_fail = False
        if OPT in cand and not ONLY:
            skip.add(OPT)
            infer = EXEC in cand and not rel.startswith(UNSAFE)
            if infer:
                skip.add(EXEC)
            subset = proxy_subset(rel, lineno, lines)
            rc_p, st_p = run_group([sys.executable, PROXY, WORK_DIR] + ([subset] if subset else []), env, 300 * SLACK)
            if st_p != "ok":
                return result(mid, cand_ms, "timeout", [], [], time.time() - t0, "proxy timeout")
            proxy_fail = rc_p != 0
        run = cand - skip
        status, rc = "ok", 0
        failed_ids, collect = set(), set()
        if run:
            ids = [BY_NUM[n]["id"] for n in sorted(run)]
            cmd = [sys.executable, "-m", "pytest", "-p", "brlabel", "-p", "no:cacheprovider",
                   "-o", "addopts=", "--timeout", "300", "-q"] + collapse_if_long(ids, run)
            rc, status = run_group(cmd, env, limit)
            if status == "ok":
                if not os.path.exists(out) or rc not in (0, 1, 2, 4) or (rc in (2, 4) and not collect_of(out)):
                    status, msg = "error", f"rc={rc} out={os.path.exists(out)}"
                else:
                    failed_ids, collect = read_label(out)
        if status != "ok":
            return result(mid, cand_ms, status, [], [], time.time() - t0, msg)
        failing = map_numbers(failed_ids, collect)
        if (rc == 0 and failing) or (rc != 0 and not failing):
            return result(mid, cand_ms, "error", [], [], time.time() - t0, f"rc={rc} but {len(failing)} failing")
        if collect:
            # a test module failed to import: find every such module in the whole suite
            co = out + ".all"
            env2 = dict(env, BRLABEL_OUT=co)
            rc2, st2 = run_group([sys.executable, "-m", "pytest", "-p", "brlabel", "-p", "no:cacheprovider",
                                  "-o", "addopts=", "--collect-only", "-q", "tests"], env2, 300)
            if st2 != "ok" or not os.path.exists(co):
                return result(mid, cand_ms, "error", [], [], time.time() - t0, "collect-all failed")
            _f, call = read_label(co)
            failing |= map_numbers(set(), call)
            msg = f"collect errors in {len(call)} files"
        if proxy_fail:
            failing |= skip
        failing = sorted(failing)
        outside = [n for n in failing if n not in cand]
        return result(mid, cand_ms, status, failing, outside, time.time() - t0, msg)
    except Exception as e:
        return result(mid, cand_ms, "error", [], [], time.time() - t0, repr(e)[:200])
    finally:
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(original)
        except Exception:
            pass
        try:
            os.remove(out)
            if os.path.exists(out + ".all"):
                os.remove(out + ".all")
        except Exception:
            pass


def main():
    global BY_ID, BY_NUM, BY_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--order", choices=["cost", "id", "ratio"], default="ratio")
    ap.add_argument("--max-label-ms", type=int, default=0)
    ap.add_argument("--first", default="", help="file of mutant ids to label before everything else")
    ap.add_argument("--deadline-s", type=int, default=0, help="stop taking new mutants after this many seconds")
    ap.add_argument("--min-cost-ms", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", default="0/1")
    a = ap.parse_args()

    tests = json.load(open(TESTS))["tests"]
    BY_ID = {t["id"]: t["number"] for t in tests}
    BY_NUM = {t["number"]: t for t in tests}
    for t in tests:
        BY_FILE.setdefault(t["file"], []).append(t["number"])

    mutants = json.load(open(os.path.join(WORK, "mutants.json")))
    sel = {}
    packed = {}
    with open(os.path.join(WORK, "sel.jsonl")) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                cand = set()
                for w in d.get("workers", []):
                    cand.update(w)
                sel[d["commit"]] = cand
                ws = [w for w in d.get("workers", []) if w]
                packed[d["commit"]] = (max(sum(round(BY_NUM[n]["seconds"] * 1000) for n in w) for w in ws)
                                       + 2000 * len(ws)) if ws else 0

    done_ids = set()
    if os.path.exists(a.out):
        with open(a.out) as f:
            for line in f:
                if line.strip():
                    try:
                        done_ids.add(json.loads(line)["id"])
                    except Exception:
                        pass

    tasks = []
    for mid, info in mutants.items():
        if mid in done_ids:
            continue
        cand = sel.get(mid, set())
        if ONLY:
            cand = cand & ONLY
        if not cand:
            continue
        cost_ms = sum(round(BY_NUM[n].get("seconds", 0.0) * 1000) for n in cand if n in BY_NUM)
        if cost_ms >= a.min_cost_ms:
            rel, lineno, old, new, op = info
            nodeids = [BY_NUM[n]["id"] for n in cand if n in BY_NUM]
            rel = info[0]
            lab = cost_ms + 3000 - (21454 if (EXEC in cand and OPT in cand and not rel.startswith(UNSAFE)) else 0)
            if a.max_label_ms and lab > a.max_label_ms:
                continue
            # value per labeling second: what the selection costs now vs what it costs to verify
            key = -(packed.get(mid, 0) - 2500) / lab if a.order == "ratio" else -cost_ms
            tasks.append((mid, key, (mid, rel, lineno, old, new, op, nodeids, cand, cost_ms)))

    first = set()
    if a.first and os.path.exists(a.first):
        first = {l.strip() for l in open(a.first) if l.strip()}
    tasks.sort(key=(lambda x: (x[0] not in first, x[1])) if a.order in ("cost", "ratio")
               else (lambda x: (x[0] not in first, x[0])))
    try:
        si, sn = (int(x) for x in a.shard.split("/"))
    except ValueError:
        si, sn = 0, 1
    if sn > 1:
        tasks = [t for i, t in enumerate(tasks) if i % sn == si]
    if a.limit:
        tasks = tasks[:a.limit]
    total = len(tasks)
    if not total:
        print("nothing to do", file=sys.stderr)
        return

    try:
        mp.set_start_method("fork", force=True)
    except RuntimeError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    t_start = time.time()
    done = empty = 0
    pool = mp.Pool(processes=a.jobs, initializer=init_worker)
    try:
        with open(a.out, "a") as fo:
            for r in pool.imap_unordered(run_one, [t[2] for t in tasks], chunksize=1):
                fo.write(json.dumps(r) + "\n")
                fo.flush()
                done += 1
                if a.deadline_s and time.time() - t_start > a.deadline_s:
                    print(f"deadline reached after {done}", file=sys.stderr, flush=True)
                    pool.terminate()
                    break
                if not r["failing"]:
                    empty += 1
                if done % 25 == 0 or done == total:
                    el = time.time() - t_start
                    print(f"progress {done}/{total} elapsed={el:.0f}s "
                          f"eta={el / done * (total - done):.0f}s empty={empty}",
                          file=sys.stderr, flush=True)
    finally:
        try:
            pool.close()
        except Exception:
            pass
        pool.join()


if __name__ == "__main__":
    main()
