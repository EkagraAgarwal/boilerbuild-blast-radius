# Blast Radius selector

Entry for the RWX "Blast Radius" track. For each commit in `/data/commits/*.patch`
(a unified diff against sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) it
prints one JSON line `{"commit": ..., "workers": [[test numbers], ...]}` choosing
the tests from `/data/tests.json` that the commit could break, packed onto workers.

## How it works

- **Per-test line coverage.** `map.json.gz` records, for every test at the pinned
  commit, which lines of `sqlglot/**/*.py` it executed (measured offline with
  `coverage`).
- **Line-level selection.** The selector parses each patch, locates the changed
  lines in the pristine source (`/sqlglot`, baked into the image), and picks the
  tests that ran those lines or the enclosing statement/definition.
- **Fallbacks.** Where line coverage is blind (new lines, import-time code such
  as class attributes, edited test modules and fixture files) it widens to the
  tests that use the affected names, read the fixture, or live in the edited
  test file. Edits whose AST is unchanged (comments, formatting) select nothing.
- **Optional run-to-verify.** When a candidate set is cheap enough, the selector
  can run the candidates against the patched code to confirm the selection.
- Chosen tests are packed onto at most 8 workers by their recorded durations.

The image (`Dockerfile`) is the kit's pinned `python:3.13-slim` base with
sqlglot's test dependencies from `requirements.lock`, sqlglot's source at the
pinned commit in `/sqlglot` (fetched from GitHub as an archive, SHA checked,
world-readable, byte-compiled at build time), and `selector.py`, `map.json.gz` (plus `runner.py` when present) in `/app`.
Env: `BR_SRC` (default `/sqlglot`), `BR_MAP` (default `map.json.gz` next to
`selector.py`), `BR_DATA` (default `/data`).

## Run locally

Needs Docker and the kit's `sample/` directory (`commits/`, `tests.json`,
`answers.json`) copied into this folder (it is not committed here):

```sh
make sample        # build, run with the scorer's flags on ./sample, score
```

Manual equivalent:

```sh
docker build --platform linux/amd64 -t blast-radius-entry .
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m \
  --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$PWD/sample/commits:/data/commits:ro" \
  --volume "$PWD/sample/tests.json:/data/tests.json:ro" \
  blast-radius-entry > output.jsonl
python3 score.py sample sample/answers.json output.jsonl
```
