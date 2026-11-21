"""Build time: table.json.gz -> table.marshal = ({key: (old_line, set_index)}, [bitmap bytes, ...]).

Distinct failing sets are stored once, as little-endian bitmaps over test numbers, so loading the
table at run time is a cheap marshal of strings and small tuples.
"""
import gzip
import json
import marshal
import sys


def main():
    src, dst = sys.argv[1], sys.argv[2]
    table = json.load(gzip.open(src, "rt"))
    sets, index, keys = [], {}, {}
    for k, (old, failing) in table.items():
        b = 0
        for n in failing:
            b |= 1 << n
        raw = b.to_bytes((b.bit_length() + 7) // 8, "little")
        if raw not in index:
            index[raw] = len(sets)
            sets.append(raw)
        keys[k] = (old, index[raw])
    with open(dst, "wb") as fh:
        marshal.dump((keys, sets), fh)
    print(f"table {len(keys)} entries, {len(sets)} distinct sets", file=sys.stderr)


if __name__ == "__main__":
    main()
