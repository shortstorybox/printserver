from falcon.testing import TestClient
import pytest


@pytest.fixture
def api() -> TestClient:
    from printserver import api

    return TestClient(api)
