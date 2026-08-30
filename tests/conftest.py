import json
from pathlib import Path

import pytest

from testsite import spec
from testsite.server import serve

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "testsite" / "manifest.json"


@pytest.fixture(scope="session", autouse=True)
def testsite():
    servers = serve()
    yield servers
    for httpd in servers:
        httpd.shutdown()


@pytest.fixture(scope="session")
def manifest():
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="session")
def base():
    return f"http://{spec.host(spec.PRIMARY)}"
