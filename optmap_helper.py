"""Reader for ``optmap.marshal.gz`` produced by ``build_optmap.py``.

The map answers: given source lines changed by a patch in ``sqlglot/**/*.py``,
which 1-based fixture pairs of a heavy optimizer fixture could execute them.
"""

import gzip
import marshal


def load_optmap(path):
    """Load a gzip+marshal optmap and return the decoded object."""
    with open(path, "rb") as f:
        return marshal.loads(gzip.decompress(f.read()))


def _read_varint(buf, i):
    """Return (value, new_index) for one unsigned LEB128 varint at buf[i:]."""
    value = 0
    shift = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    raise ValueError("truncated varint")


def _decode_ints(buf):
    """Inverse of build_optmap._encode_ints -> sorted tuple of ints."""
    if not buf:
        return ()
    mode = buf[0]
    i = 1
    if mode == 0:  # every query 1..n
        n, _ = _read_varint(buf, i)
        return tuple(range(1, n + 1))
    if mode == 1:  # contiguous runs (start, length)
        nruns, i = _read_varint(buf, i)
        out = []
        for _ in range(nruns):
            start, i = _read_varint(buf, i)
            length, i = _read_varint(buf, i)
            out.extend(range(start, start + length))
        return tuple(out)
    if mode == 2:  # delta varint
        out = []
        acc = 0
        n = len(buf)
        while i < n:
            d, i = _read_varint(buf, i)
            acc += d
            out.append(acc)
        return tuple(out)
    if mode == 3:  # bitmap over 1..8*len(buf)
        out = []
        for byte_i, byte in enumerate(buf[i:]):
            for bit in range(8):
                if byte & (1 << bit):
                    out.append(byte_i * 8 + bit + 1)
        return tuple(out)
    raise ValueError("unknown optmap encoding mode %d" % mode)


def _norm_fixture(name):
    """Accept both ``optimizer/simplify.sql`` and ``simplify``."""
    if name.endswith(".sql"):
        name = name[:-4]
    if name.startswith("optimizer/"):
        name = name[len("optimizer/"):]
    return name


def queries_for_opt_fixture(optmap, fixture_rel, changed_lines_by_rel):
    """Return ``(sorted_1based_queries, touches_setup)`` for one fixture.

    ``fixture_rel`` may be ``"optimizer/simplify.sql"`` or just ``"simplify"``.
    ``changed_lines_by_rel`` maps a ``sqlglot/...`` relative path to an
    iterable of changed line numbers.

    ``touches_setup`` is True when at least one changed line was executed
    during harness setup/import (in which case it affects every query).
    """
    files = optmap["files"]
    fidx = {rel: j for j, rel in enumerate(files)}
    fixtures = {_norm_fixture(k): v for k, v in optmap["fixtures"].items()}
    per_file = fixtures.get(_norm_fixture(fixture_rel), {})
    setup = optmap["setup"]

    touched = set()
    touches_setup = False
    for rel, lines in changed_lines_by_rel.items():
        fi = fidx.get(rel)
        if fi is None:
            continue
        setup_lines = set(_decode_ints(setup.get(fi, b"")))
        fmap = per_file.get(fi, {})
        for line in lines:
            line = int(line)
            if line in setup_lines:
                touches_setup = True
            buf = fmap.get(line)
            if buf is not None:
                touched.update(_decode_ints(buf))
    return (sorted(touched), touches_setup)


def decode_optmap(optmap):
    """Fully decode an optmap into plain Python containers (for inspection).

    Returns ``(files, total, setup, fixtures)`` where setup maps rel path to
    sorted line tuple and fixtures maps fixture -> rel -> line -> query tuple.
    """
    files = list(optmap["files"])
    setup = {
        files[fi]: _decode_ints(buf) for fi, buf in optmap["setup"].items()
    }
    fixtures = {}
    for name, fd in optmap["fixtures"].items():
        out = {}
        for fi, per_line in fd.items():
            out[files[fi]] = {ln: _decode_ints(buf) for ln, buf in per_line.items()}
        fixtures[name] = out
    return files, dict(optmap["total"]), setup, fixtures
