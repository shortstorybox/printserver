from falcon.testing import TestClient


def test_index_page_load(api: TestClient):
    response = api.get("/")
    assert response.status_code == 200


def test_list_printers_loads(api: TestClient):
    response = api.get("/printers")
    assert response.status_code == 200
    assert "printers" in response.json
    assert isinstance(response.json["printers"], list)


def test_domains_approve_loads(api: TestClient):
    response = api.get("/domains/approve?origin=https://example.com")
    assert response.status_code == 200
