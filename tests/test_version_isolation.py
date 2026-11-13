"""Same file names are independent across model versions, including pending changes."""

import json
from contextlib import ExitStack
from hashlib import sha256
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

SCOPES = (("M910", "high"), ("M910", "low"), ("M920", "high"))
CONTENTS = (b'{"scope":"M910-high"}', b'{"scope":"M910-low"}', b'{"scope":"M920-high"}')
CURVE_FIELDS = {"curve_type": "thermal", "description": "version isolation"}


@pytest.fixture
def isolated_hub(tmp_path):
    from projecthub.application import AppConfig, resolve_settings
    from projecthub.database import Database
    from projecthub.files import FileService
    from projecthub.permissions import Permissions
    from projecthub.projects import ProjectService

    settings = resolve_settings(AppConfig(project_root=tmp_path))
    db = Database(settings)
    permissions = Permissions(db)
    files = FileService(settings, db, permissions)
    projects = ProjectService(settings, db, permissions, files)
    db.open(initialize=files.scan_initial)
    try:
        projects.initialize()
        files.initialize()
        actor = {"user_id": db.one("SELECT id FROM users WHERE is_admin=1")["id"]}
        for name in dict.fromkeys(name for name, _ in SCOPES):
            projects.create_project_model(name, actor=actor)
        versions = [projects.create_project_version(name, version, actor=actor) for name, version in SCOPES]
        yield files, actor, versions
    finally:
        files.close()
        db.close()


def finish_upload(handle, content):
    try:
        handle.write(content)
        return handle.finish()
    finally:
        handle.close()


def download(handle):
    with handle:
        return b"".join(iter(handle.read_chunk, b""))


def assert_formal_files(files, actor, versions, records, contents, category):
    for (name, version), project, record, content in zip(SCOPES, versions, records, contents):
        listing = files.catalog.list_files(project_id=project["id"], category=category, actor=actor)
        if content is None:
            assert listing == {"files": [], "count": 0}
            assert not (files.settings.storage.object_dir / record["path"]).exists()
            continue
        assert listing["count"] == 1
        actual = listing["files"][0]
        assert actual["id"] == record["id"]
        assert actual["project_id"] == project["id"]
        assert actual["path"] == f"{name}/{version}/{category}/shared.json"
        assert actual["sha256"] == sha256(content).hexdigest()
        assert (files.settings.storage.object_dir / actual["path"]).read_bytes() == content
        assert (
            download(files.catalog.prepare_download(record["id"], category, project["id"], actor=actor))
            == content
        )
        assert files.catalog.read_json(record["id"], category, project["id"], actor=actor) == json.loads(
            content
        )


def test_curve_pending_approval_update_and_delete_are_version_local(isolated_hub):
    from projecthub.contracts import HubError

    files, actor, versions = isolated_hub
    # Keep every upload open together: same-name reservations must also be version-local.
    with ExitStack() as cleanup:
        handles = []
        for project, content in zip(versions, CONTENTS):
            handle = files.begin_upload(
                actor, project["id"], "curve", "shared.json", CURVE_FIELDS, sha256(content).hexdigest()
            )
            cleanup.callback(handle.close)
            handles.append(handle)
        pending = [finish_upload(handle, content) for handle, content in zip(handles, CONTENTS)]
    assert len({row["path"] for row in pending}) == 3
    for (name, version), project, review, content in zip(SCOPES, versions, pending, CONTENTS):
        assert review["path"] == f"{name}/{version}/curve/shared.json"
        assert (files.settings.storage.audit_dir / review["path"]).read_bytes() == content
        assert download(files.reviews.prepare_review_download(actor, review["id"])) == content
        listing = files.reviews.list_reviews(actor, project_id=project["id"])
        assert [row["id"] for row in listing["reviews"]] == [review["id"]]
        assert files.catalog.list_files(project_id=project["id"], category="curve", actor=actor)["count"] == 0

    records = [None] * len(versions)
    for index in (1, 0, 2):
        records[index] = files.reviews.approve_review(actor, pending[index]["id"])
        assert not (files.settings.storage.audit_dir / pending[index]["path"]).exists()
        for remaining in files.reviews.list_reviews(actor)["reviews"]:
            expected = CONTENTS[
                next(i for i, project in enumerate(versions) if project["id"] == remaining["project_id"])
            ]
            assert download(files.reviews.prepare_review_download(actor, remaining["id"])) == expected
    assert_formal_files(files, actor, versions, records, CONTENTS, "curve")

    # A valid file_id cannot be substituted into another version, even by an administrator.
    for owner, record in zip(versions, records):
        for target in versions:
            if target["id"] == owner["id"]:
                continue
            for read in (files.catalog.read_json, files.catalog.prepare_download):
                with pytest.raises(HubError) as caught:
                    read(record["id"], "curve", target["id"], actor=actor)
                assert caught.value.code == "FILE_NOT_FOUND"
            with pytest.raises(HubError) as caught:
                files.begin_curve_update(
                    actor, target["id"], record["id"], CURVE_FIELDS, sha256(b"{}").hexdigest()
                )
            assert caught.value.code == "FILE_NOT_FOUND"
            with pytest.raises(HubError) as caught:
                files.reviews.request_curve_delete(actor, target["id"], record["id"])
            assert caught.value.code == "FILE_NOT_FOUND"
    assert files.reviews.list_reviews(actor)["count"] == 0

    replacements = tuple(
        content.replace(b"high", b"high-updated").replace(b"low", b"low-updated") for content in CONTENTS
    )
    updates = [
        finish_upload(
            files.begin_curve_update(
                actor, project["id"], record["id"], CURVE_FIELDS, sha256(content).hexdigest()
            ),
            content,
        )
        for project, record, content in zip(versions, records, replacements)
    ]
    assert_formal_files(files, actor, versions, records, CONTENTS, "curve")
    for review, content in zip(updates, replacements):
        assert download(files.reviews.prepare_review_download(actor, review["id"])) == content
    assert files.reviews.approve_review(actor, updates[0]["id"])["id"] == records[0]["id"]
    files.reviews.reject_review(actor, updates[1]["id"])
    assert_formal_files(files, actor, versions, records, (replacements[0], CONTENTS[1], CONTENTS[2]), "curve")
    assert download(files.reviews.prepare_review_download(actor, updates[2]["id"])) == replacements[2]

    deletion = files.reviews.request_curve_delete(actor, versions[0]["id"], records[0]["id"])
    assert download(files.reviews.prepare_review_download(actor, deletion["id"])) == replacements[0]
    assert files.reviews.approve_review(actor, deletion["id"])["status"] == "deleted"
    assert_formal_files(files, actor, versions, records, (None, CONTENTS[1], CONTENTS[2]), "curve")
    assert download(files.reviews.prepare_review_download(actor, updates[2]["id"])) == replacements[2]
    assert files.reviews.approve_review(actor, updates[2]["id"])["id"] == records[2]["id"]
    assert_formal_files(files, actor, versions, records, (None, CONTENTS[1], replacements[2]), "curve")
    assert files.reviews.list_reviews(actor)["count"] == 0


@pytest.mark.parametrize("category", ["public", "internal"])
def test_direct_same_name_files_delete_and_reupload_are_version_local(isolated_hub, category):
    files, actor, versions = isolated_hub
    records = [
        finish_upload(
            files.begin_upload(
                actor,
                project["id"],
                category,
                "shared.json",
                {"description": ""},
                sha256(content).hexdigest(),
            ),
            content,
        )
        for project, content in zip(versions, CONTENTS)
    ]
    assert_formal_files(files, actor, versions, records, CONTENTS, category)
    assert (
        files.delete_managed_file(actor, versions[1]["id"], category, records[1]["id"])["status"] == "deleted"
    )
    assert_formal_files(files, actor, versions, records, (CONTENTS[0], None, CONTENTS[2]), category)
    replacement = b'{"scope":"M910-low-replaced"}'
    records[1] = finish_upload(
        files.begin_upload(
            actor,
            versions[1]["id"],
            category,
            "shared.json",
            {"description": ""},
            sha256(replacement).hexdigest(),
        ),
        replacement,
    )
    assert_formal_files(files, actor, versions, records, (CONTENTS[0], replacement, CONTENTS[2]), category)


def command(data):
    import rfc8785

    return {"id": "version-isolation", "data": data, "sha256": sha256(rfc8785.dumps(data)).hexdigest()}


def response_data(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def test_http_same_name_files_and_cross_version_references(tmp_path):
    from projecthub.application import AppConfig, create_app

    app = create_app(AppConfig(project_root=tmp_path))
    with TestClient(app) as client:
        admin = app.state.db.one("SELECT name,password FROM users WHERE is_admin=1")
        client.headers.update(
            {"X-User-Name": quote(admin["name"], safe=""), "X-Password": quote(admin["password"], safe="")}
        )
        models = {
            name: response_data(client.post("/api/v1/projects", json=command({"name": name})), 201)["id"]
            for name in dict.fromkeys(name for name, _ in SCOPES)
        }
        versions, bases, pending = [], [], []
        for (name, version), content in zip(SCOPES, CONTENTS):
            version_id = response_data(
                client.post(f"/api/v1/projects/{models[name]}/versions", json=command({"version": version})),
                201,
            )["id"]
            versions.append(version_id)
            base = f"/api/v1/projects/{models[name]}/versions/{version_id}"
            bases.append(base)
            review = response_data(
                client.post(
                    base + "/curves/upload",
                    params={
                        "file_name": "shared.json",
                        **CURVE_FIELDS,
                        "file_sha256": sha256(content).hexdigest(),
                    },
                    content=content,
                    headers={"Content-Type": "application/json"},
                ),
                202,
            )
            pending.append(review)
        for (name, _), version_id, review, content in zip(SCOPES, versions, pending, CONTENTS):
            listing = response_data(
                client.get("/api/v1/reviews", params={"model_id": models[name], "version_id": version_id})
            )
            assert [row["id"] for row in listing["reviews"]] == [review["id"]]
            response = client.get(f"/api/v1/reviews/{review['id']}/download")
            assert response.status_code == 200
            assert response.content == content
        records = [
            response_data(client.post(f"/api/v1/reviews/{review['id']}/approve", json=command({})))
            for review in pending
        ]
        for base, record, content in zip(bases, records, CONTENTS):
            listing = response_data(client.get(base + "/files/curve"))
            assert [row["id"] for row in listing["files"]] == [record["id"]]
            response = client.get(base + f"/files/curve/{record['id']}/download")
            assert response.status_code == 200
            assert response.content == content
            assert response.headers["x-content-sha256"] == sha256(content).hexdigest()
            for other in records:
                if other["id"] == record["id"]:
                    continue
                for suffix in ("json", "download"):
                    response = client.get(base + f"/files/curve/{other['id']}/{suffix}")
                    assert response_data(response, 404)["error"]["code"] == "FILE_NOT_FOUND"
                response = client.head(base + f"/files/curve/{other['id']}/download")
                assert response.status_code == 404 and response.content == b""
                response = client.post(base + "/files/curve/json", json=command({"file_id": other["id"]}))
                assert response_data(response, 404)["error"]["code"] == "FILE_NOT_FOUND"
        wrong_model_base = f"/api/v1/projects/{models['M920']}/versions/{versions[0]}"
        response = client.get(wrong_model_base + f"/files/curve/{records[0]['id']}/download")
        assert response_data(response, 404)["error"]["code"] == "PROJECT_NOT_FOUND"


def test_firmware_upgrade_target_is_limited_to_its_version(isolated_hub):
    files, actor, versions = isolated_hub
    contents = (b"M910-high-firmware-v1", b"M910-low-firmware-v1", b"M920-high-firmware-v1")
    fields = {
        "firmware_type": "controller",
        "version": "1.0",
        "release_notes": "initial release",
        "min_version": "0.1",
        "max_version": "0.9",
        "supported_models": ["M910", "M920"],
    }
    # Identical names, firmware types and compatibility ranges coexist while pending.
    pending = [
        finish_upload(
            files.begin_upload(
                actor, project["id"], "firmware", "shared.bin", fields, sha256(content).hexdigest()
            ),
            content,
        )
        for project, content in zip(versions, contents)
    ]
    assert files.reviews.list_reviews(actor)["count"] == 3
    for (name, version), project, review, content in zip(SCOPES, versions, pending, contents):
        assert review["path"] == f"{name}/{version}/firmware/shared.bin"
        assert (files.settings.storage.audit_dir / review["path"]).read_bytes() == content
        assert download(files.reviews.prepare_review_download(actor, review["id"])) == content
        listing = files.reviews.list_reviews(actor, project_id=project["id"])
        assert [row["id"] for row in listing["reviews"]] == [review["id"]]
    records = [files.reviews.approve_review(actor, review["id"]) for review in pending]
    snapshots = [
        files.catalog.list_files(project_id=project["id"], category="firmware", actor=actor)
        for project in versions
    ]
    for project, record, snapshot, content in zip(versions, records, snapshots, contents):
        assert snapshot["count"] == 1
        assert snapshot["files"][0]["id"] == record["id"]
        assert (
            download(files.catalog.prepare_download(record["id"], "firmware", project["id"], actor=actor))
            == content
        )
        available = files.catalog.check_firmware(project["id"], "0.5", actor=actor)
        assert available["update_available"] is True
        assert [row["id"] for row in available["firmwares"]] == [record["id"]]

    replacement = b"M910-high-firmware-v2"
    upgraded_fields = {
        **fields,
        "version": "2.0",
        "min_version": "1.0",
        "max_version": "1.9",
        "release_notes": "high upgrade",
    }
    update = finish_upload(
        files.begin_upload(
            actor,
            versions[0]["id"],
            "firmware",
            "shared.bin",
            upgraded_fields,
            sha256(replacement).hexdigest(),
        ),
        replacement,
    )
    assert update["field_info"]["action"] == "update"
    assert update["field_info"]["file_id"] == records[0]["id"]
    for project, snapshot in zip(versions, snapshots):
        assert (
            files.catalog.list_files(project_id=project["id"], category="firmware", actor=actor) == snapshot
        )
    upgraded = files.reviews.approve_review(actor, update["id"])
    assert upgraded["id"] == records[0]["id"]
    assert upgraded["path"] == "M910/high/firmware/shared.bin"
    assert {key: upgraded[key] for key in upgraded_fields} == upgraded_fields
    assert upgraded["sha256"] == sha256(replacement).hexdigest()
    assert (
        download(files.catalog.prepare_download(upgraded["id"], "firmware", versions[0]["id"], actor=actor))
        == replacement
    )
    for project, record, snapshot, content in zip(versions[1:], records[1:], snapshots[1:], contents[1:]):
        assert (
            files.catalog.list_files(project_id=project["id"], category="firmware", actor=actor) == snapshot
        )
        assert (files.settings.storage.object_dir / record["path"]).read_bytes() == content
        assert (
            download(files.catalog.prepare_download(record["id"], "firmware", project["id"], actor=actor))
            == content
        )
        assert files.catalog.check_firmware(project["id"], "1.0", actor=actor)["update_available"] is False
    available = files.catalog.check_firmware(versions[0]["id"], "1.0", actor=actor)
    assert available["update_available"] is True
    assert [row["id"] for row in available["firmwares"]] == [records[0]["id"]]
    assert files.reviews.list_reviews(actor)["count"] == 0
