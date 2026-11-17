"""The curve-name HTTP contract is scoped to approved curves in one version."""

from hashlib import sha256

import pytest
from test_http import client as client
from test_http import command
from test_model_permissions import setup_models


def upload_curve(client, base, filename, curve_name):
    body = b'{"point":1}'
    response = client.post(
        base + "/curves/upload",
        params={
            "file_name": filename,
            "curve_name": curve_name,
            "curve_type": "test",
            "description": "",
            "file_sha256": sha256(body).hexdigest(),
        },
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202, response.text
    review = response.json()["data"]
    assert review["field_info"]["fields"]["curve_name"] == curve_name
    return review


def approve(client, review):
    response = client.post(f"/api/v1/reviews/{review['id']}/approve", json=command({}))
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("field", ["curve_name", "file_name"])
@pytest.mark.parametrize("name", [None, "", " \t\u3000 "])
def test_new_curve_upload_requires_both_nonblank_names(client, field, name):
    _, model, _, _, versions, _, _, _ = setup_models(client)
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    body = b"{}"
    params = {
        "file_name": "curve.json",
        "curve_name": "Curve",
        "curve_type": "test",
        "description": "",
        "file_sha256": sha256(body).hexdigest(),
    }
    if name is None:
        params.pop(field)
    else:
        params[field] = name
    response = client.post(
        base + "/curves/upload", params=params, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert client.app.state.db.query("SELECT id FROM pending_reviews") == []


def test_names_list_uses_approved_names_and_current_version_permissions(client):
    admin, model, other, _, versions, hidden, actor, headers = setup_models(client)
    state = client.app.state
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    second = f"/api/v1/projects/{model['id']}/versions/{versions[1]['id']}"
    other_base = f"/api/v1/projects/{other['id']}/versions/{hidden['id']}"
    names_path = base + "/curves/names"
    pending = [
        upload_curve(client, base, f"{index}.json", name) for index, name in enumerate(("beta", "Alpha"))
    ]
    assert client.get(names_path).json()["data"] == {"names": [], "count": 0}
    published = [approve(client, review) for review in pending]
    assert [row["curve_name"] for row in published] == ["beta", "Alpha"]
    approve(client, upload_curve(client, second, "separate.json", "OtherVersion"))
    approve(client, upload_curve(client, other_base, "private.json", "OtherModel"))
    assert client.get(names_path).json()["data"] == {"names": ["Alpha", "beta"], "count": 2}
    assert client.get(second + "/curves/names").json()["data"] == {"names": ["OtherVersion"], "count": 1}
    assert client.get(names_path, headers=headers).status_code == 404
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"dealer": ["M910"]})
    assert client.get(names_path, headers=headers).status_code == 403
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"curve.read": ["M910"]})
    assert client.get(names_path, headers=headers).json()["data"] == {"names": ["Alpha", "beta"], "count": 2}
    assert client.get(other_base + "/curves/names", headers=headers).status_code == 404
    wrong_owner = f"/api/v1/projects/{other['id']}/versions/{versions[0]['id']}/curves/names"
    assert client.get(wrong_owner).status_code == 404
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {})
    assert client.get(names_path, headers=headers).status_code == 404


def test_replacement_name_changes_only_on_approval_and_can_be_omitted(client):
    _, model, _, _, versions, _, _, _ = setup_models(client)
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    curve = approve(client, upload_curve(client, base, "original.json", "Original"))
    body = b'{"point":2}'
    params = {
        "curve_name": "Renamed",
        "curve_type": "test",
        "description": "",
        "file_sha256": sha256(body).hexdigest(),
    }
    response = client.put(
        base + f"/curves/{curve['id']}",
        params=params,
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202, response.text
    assert client.get(base + "/curves/names").json()["data"]["names"] == ["Original"]
    updated = approve(client, response.json()["data"])
    assert updated["curve_name"] == "Renamed" and updated["file_name"] == "original.json"
    params.pop("curve_name")
    response = client.put(
        base + f"/curves/{curve['id']}",
        params=params,
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["data"]["field_info"]["fields"]["curve_name"] == "Renamed"
    approve(client, response.json()["data"])
    assert client.get(base + "/curves/names").json()["data"] == {"names": ["Renamed"], "count": 1}


def test_curve_name_and_file_name_are_independently_unique_in_one_version(client):
    _, model, _, _, versions, _, _, _ = setup_models(client)
    base = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}"
    sibling = f"/api/v1/projects/{model['id']}/versions/{versions[1]['id']}"
    original = upload_curve(client, base, "original.json", "Shared")

    def attempt(file_name, curve_name):
        body = b"{}"
        return client.post(
            base + "/curves/upload",
            params={
                "file_name": file_name,
                "curve_name": curve_name,
                "curve_type": "test",
                "description": "",
                "file_sha256": sha256(body).hexdigest(),
            },
            content=body,
            headers={"Content-Type": "application/json"},
        )

    for filename, name in (("another.json", "Shared"), ("ORIGINAL.JSON", "Different")):
        response = attempt(filename, name)
        assert response.status_code == 409, response.text
        assert response.json()["data"]["error"]["code"] == "REVIEW_ALREADY_PENDING"
    assert len(client.app.state.db.query("SELECT id FROM pending_reviews")) == 1
    approve(client, original)
    for filename, name, code in (
        ("another.json", "Shared", "CURVE_NAME_ALREADY_EXISTS"),
        ("ORIGINAL.JSON", "Different", "FILE_ALREADY_EXISTS"),
    ):
        response = attempt(filename, name)
        assert response.status_code == 409, response.text
        assert response.json()["data"]["error"]["code"] == code
    other = approve(client, upload_curve(client, base, "other.json", "Other"))
    body = b"{}"
    response = client.put(
        base + f"/curves/{other['id']}",
        params={
            "curve_name": "Shared",
            "curve_type": "test",
            "description": "",
            "file_sha256": sha256(body).hexdigest(),
        },
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 409
    assert response.json()["data"]["error"]["code"] == "CURVE_NAME_ALREADY_EXISTS"
    approve(client, upload_curve(client, sibling, "original.json", "Shared"))
    assert client.get(sibling + "/curves/names").json()["data"] == {"names": ["Shared"], "count": 1}
    assert client.get(base + "/curves/names").json()["data"] == {"names": ["Other", "Shared"], "count": 2}
