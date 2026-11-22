# Blast Radius selector image. The scorer builds this on linux/amd64 with
# network, then runs it read-only, offline, as user 65534.
# Base digest = the image every commit was labeled in (its Python and SQLite
# decide which sqlglot tests run), so keep it exactly.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    SKIP_INTEGRATION=0 \
    BR_SRC=/sqlglot

# sqlglot's test dependencies, pinned as in the labeling environment.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# Pristine sqlglot at the pinned commit, fetched from sqlglot's own GitHub repo
# (no git/curl in slim, so urllib). The archive's folder name must carry the
# full SHA. Keeps sqlglot/, tests/, the packaging files and sqlglotc/ (its
# setup.py is loaded by tests/test_sqlglotc.py). It is byte-compiled once here;
# nothing writes .pyc at runtime (PYTHONDONTWRITEBYTECODE).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, os, shutil, tarfile, urllib.request as u; \
sha = '${SQLGLOT_SHA}'; \
url = f'https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz'; \
tf = tarfile.open(fileobj=io.BytesIO(u.urlopen(url, timeout=300).read()), mode='r:gz'); \
tops = {m.name.split('/', 1)[0] for m in tf.getmembers()}; \
assert len(tops) == 1, tops; top = tops.pop(); \
assert sha in top, f'archive folder {top} lacks {sha}'; \
tf.extractall('/tmp/sg', filter='data'); \
shutil.move('/tmp/sg/' + top, '/sqlglot'); shutil.rmtree('/tmp/sg'); \
[shutil.rmtree('/sqlglot/' + d, ignore_errors=True) for d in ('benchmarks', 'posts', 'docs', 'pdoc', 'sqlglot-integration-tests', '.github')]; \
assert all(os.path.isdir('/sqlglot/' + d) for d in ('sqlglot', 'tests')); \
print('sqlglot', top, sorted(os.listdir('/sqlglot')))" \
 && python -m compileall -q /sqlglot/sqlglot /sqlglot/tests \
 && chmod -R a+rX /sqlglot

# Build index + mutant table BEFORE copying selector code so code edits rebuild in ~1s.
COPY build_index.py mk_table.py build_tpc_ops.py map.json.gz collect.json.gz querymap.json.gz table.json.gz exprmap.json.gz /app/
RUN python /app/build_index.py /sqlglot /app/index.marshal /app/map.json.gz \
 && python /app/mk_table.py /app/table.json.gz /app/table.marshal \
 && python /app/build_tpc_ops.py /sqlglot /app/tpc_ops.json
COPY selector.py runner.py partial.py tpcds_proxy.py regdiff.py probe.py /app/
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV BR_PROBE=keeptable
CMD ["python", "/app/probe.py"]
