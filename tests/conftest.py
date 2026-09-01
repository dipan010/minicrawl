import json
from pathlib import Path

import pytest

from testsite import spec
from testsite.server import serve

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "testsite" / "manifest.json"


@pytest.fixture(scope="session", autouse=True)
def testsite():
    """Start the corpus, sharing one that is already running.

    The README tells you to run `python -m testsite.server` in another
    terminal; without skip_busy the whole suite then fails to collect with
    "Address already in use", which looks like 151 broken tests rather than one
    occupied socket. Only the servers this fixture actually started get shut
    down."""
    servers = serve(skip_busy=True)
    yield servers
    for httpd in servers:
        httpd.shutdown()


@pytest.fixture(scope="session")
def manifest():
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="session")
def base():
    return f"http://{spec.host(spec.PRIMARY)}"
