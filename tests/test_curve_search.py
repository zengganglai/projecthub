"""Curve names use literal substring matching within the authorized version."""

from test_curve_names_http import approve, upload_curve
from test_http import client as client
from test_model_permissions import setup_models


def test_curve_name_search_contains_partial_text_and_remains_composable_and_scoped(client):
    admin, model, other, _, versions, hidden, actor, headers = setup_models(client)
    state = client.app.state
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    sibling = f"/api/v1/projects/{model['id']}/versions/{versions[1]['id']}"
    private = f"/api/v1/projects/{other['id']}/versions/{hidden['id']}"
    first = approve(client, upload_curve(client, base, "selected.json", "Name%_"))
    second = approve(client, upload_curve(client, base, "longer.json", "Name%_extra"))
    third = approve(client, upload_curve(client, base, "lower.json", "name%_"))
    fourth = approve(client, upload_curve(client, base, "temperature.json", "温度校准曲线"))
    fifth = approve(client, upload_curve(client, base, "pressure.json", "压力校准曲线"))
    approve(client, upload_curve(client, sibling, "selected.json", "Name%_"))
    approve(client, upload_curve(client, private, "selected.json", "Name%_"))
    endpoint = base + "/files/curve"
    assert client.get(endpoint).json()["data"]["count"] == 5
    for filters, identifiers in (
        ({"curve_name": "Name%_"}, [first["id"], second["id"]]),
        ({"curve_name": "Name%_extra"}, [second["id"]]),
        ({"curve_name": "name%_"}, [third["id"]]),
        ({"curve_name": "Name"}, [first["id"], second["id"]]),
        ({"curve_name": "extra"}, [second["id"]]),
        ({"curve_name": "%_e"}, [second["id"]]),
        ({"curve_name": "%_"}, [first["id"], second["id"], third["id"]]),
        ({"curve_name": "校准"}, [fourth["id"], fifth["id"]]),
        ({"curve_name": "温度"}, [fourth["id"]]),
        ({"curve_name": ""}, [first["id"], second["id"], third["id"], fourth["id"], fifth["id"]]),
        ({"curve_name": "missing"}, []),
        (
            {"curve_name": "Name%_", "file_name": "selected.json", "file_type": "test", "q": "SELECTED"},
            [first["id"]],
        ),
        ({"curve_name": "Name%_", "file_name": "longer.json"}, [second["id"]]),
        ({"curve_name": "Name%_", "file_type": "other"}, []),
        ({"curve_name": "Name%_", "q": "no-match"}, []),
    ):
        response = client.get(endpoint, params=filters)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert [row["id"] for row in data["files"]] == identifiers, filters
        assert data["count"] == len(identifiers)
    filters = {"curve_name": "Name%_"}
    assert client.get(endpoint, params=filters, headers=headers).status_code == 404
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"curve.read": ["M910"]})
    response = client.get(endpoint, params=filters, headers=headers)
    assert [row["id"] for row in response.json()["data"]["files"]] == [first["id"], second["id"]]
    assert client.get(private + "/files/curve", params=filters, headers=headers).status_code == 404
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {})
    assert client.get(endpoint, params=filters, headers=headers).status_code == 404


def test_curve_name_filter_is_rejected_for_other_file_categories(client):
    _, model, _, _, versions, _, _, _ = setup_models(client)
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    for category in ("public", "firmware", "internal"):
        response = client.get(base + f"/files/{category}", params={"curve_name": "Name"})
        assert response.status_code == 422
        assert response.json()["data"]["error"]["code"] == "INVALID_FILTER"
