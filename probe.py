"""Measurement probe (practice board only, never a final pick).

Runs the real selector unchanged, then rewrites its stdout per BR_PROBE so that the
board's simulated seconds / miss count reveal where our practice cost goes:

  census     every commit -> one marker worker whose cost encodes the commit's cost class.
             misses ~= number of truly failing commits (F);
             sim - 300*misses = sum_i n_i * (2000 + D_i)  -> class counts n_i.
  keep:<c>   keep our real selection for commits in class(es) <c> (e.g. keep:1 or keep:23),
             print empty for all others.  misses = failing commits outside <c>;
             sim - 300*misses = our exact cost on <c>.

Cost classes from our own packed cost c (ms): 0: c==0, 1: c<=3000, 2: 3000<c<10000, 3: c>=10000.
"""
import json
import re
import os
import subprocess
import sys

MODE = os.environ.get("BR_PROBE", "census")
DATA = os.environ.get("BR_DATA", "/data")
D = {0: 0, 1: 1, 2: 41, 3: 1681}  # marker extra ms per class


def cls(c):
    if c == 0:
        return 0
    if c <= 3000:
        return 1
    if c < 10000:
        return 2
    return 3


def pick(ms, target, avoid):
    """Tests whose durations sum to exactly `target` ms, smallest tests first (rarely failing)."""
    if target == 0:
        for n, d in sorted(ms.items()):
            if d == 0 and n not in avoid:
                return [n]
        return []
    items = sorted(((d, n) for n, d in ms.items() if 0 < d <= target and n not in avoid))
    # dp over sums using small tests only
    reach = {0: []}
    for d, n in items:
        for s in list(reach.keys()):
            t = s + d
            if t <= target and t not in reach:
                reach[t] = reach[s] + [n]
        if target in reach:
            return reach[target]
    return None


def main():
    p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "selector.py")],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = p.stderr.decode(errors="replace")
    sys.stderr.write(err)
    why = {}
    for l in err.splitlines():
        m = re.match(r"^(\S+): \d+ tests, ", l)
        if m:
            why[m.group(1)] = l
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    lines = [json.loads(l) for l in p.stdout.decode().splitlines() if l.strip()]
    markers = {}
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    out = []
    for o in lines:
        ws = [w for w in o["workers"] if w]
        c = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
        k = cls(c)
        counts[k] += 1
        if MODE == "census":
            if k not in markers:
                markers[k] = pick(ms, D[k], set())
            m = markers[k]
            out.append({"commit": o["commit"], "workers": [m] if m is not None else []})
        elif MODE == "census2":
            r = why.get(o["commit"], "")
            tab = "table sqlglot/" in r or "table tests/" in r
            d = 0 if k == 0 else (1 if tab else (41 if k >= 2 else 1681))
            if d not in markers:
                markers[d] = pick(ms, d, set())
            out.append({"commit": o["commit"], "workers": [markers[d]]})
        elif MODE == "keeptable":
            r = why.get(o["commit"], "")
            tab = "table sqlglot/" in r or "table tests/" in r
            out.append({"commit": o["commit"], "workers": ws if tab else []})
        elif MODE.startswith("keep3drop:"):
            drop = {int(x) for x in MODE[10:].split(",")}
            ws2 = [[n for n in w if n not in drop] for w in ws] if k == 3 else []
            out.append({"commit": o["commit"], "workers": [w for w in ws2 if w]})
        elif MODE.startswith("keep:"):
            keep = {int(ch) for ch in MODE[5:]}
            out.append({"commit": o["commit"], "workers": ws if k in keep else []})
        else:
            out.append({"commit": o["commit"], "workers": ws})
    print(f"probe {MODE}: class counts {counts} markers {markers}", file=sys.stderr)
    sys.stdout.write("".join(json.dumps(x) + "\n" for x in out))


if __name__ == "__main__":
    main()
