"""pytest plugin: write the nodeids of failing tests (any phase, any subtest, collection errors).

Output: $BRLABEL_OUT (default /tmp/failing.json) = {"failed": [nodeid...], "collect_errors": [path...]}
xdist-safe: each worker appends to its own file; the controller merges.
"""

import json
import os

OUT = os.environ.get("BRLABEL_OUT", "/tmp/failing.json")
_failed = set()
_collect = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _collect.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    path = OUT + (f".{os.getpid()}" if os.environ.get("PYTEST_XDIST_WORKER") else "")
    with open(path, "w") as f:
        json.dump({"failed": sorted(_failed), "collect_errors": sorted(_collect)}, f)
