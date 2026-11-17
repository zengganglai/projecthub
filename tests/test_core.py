from dataclasses import replace
from hashlib import sha256

import pytest


def test_strict_json_and_digest():
    from projecthub.contracts import decode_json, digest_data, encode_response

    assert decode_json(b'{"x":1}') == {"x": 1}
    assert digest_data({"b": 2, "a": 1}) == sha256(b'{"a":1,"b":2}').hexdigest()
    for data in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'"\xff"']:
        with pytest.raises(ValueError):
            decode_json(data)
    result = encode_response(0, 200, {"a": 1})
    assert set(result) == {"id", "status", "timestamp", "data", "sha256"}
    assert result["id"] == 0


def test_config_normalizes_atomically_without_creating_data(tmp_path):
    from projecthub.application import AppConfig, resolve_settings
    from projecthub.contracts import HubError

    original = AppConfig(project_root=tmp_path)
    configured = resolve_settings(original)
    assert configured.database.path == tmp_path / "data/sql/device_bridge.sqlite3"
    assert configured.http.max_concurrent_requests == 16
    assert configured.users.max_users == 200
    for user_limit in (0, True, 201):
        with pytest.raises(HubError):
            resolve_settings(replace(original, users=replace(original.users, max_users=user_limit)))
    assert not (tmp_path / "data").exists()
    for count in [0, -1, True, 1.5]:
        with pytest.raises(HubError):
            resolve_settings(replace(original, http=replace(original.http, max_concurrent_requests=count)))
    with pytest.raises(HubError):
        resolve_settings(
            replace(original, database=replace(original.database, path=tmp_path.parent / "other.sqlite"))
        )


def test_path_and_version_boundaries(tmp_path):
    from projecthub.contracts import HubError
    from projecthub.files.rules import version_key
    from projecthub.storage import project_name, safe_path

    assert safe_path(tmp_path.resolve(), "M/v1/curve/a.json") == tmp_path / "M/v1/curve/a.json"
    for value in ["../outside", "a/../b", "C:/secret", "a\\b", "/absolute", "CON/a"]:
        with pytest.raises(HubError):
            safe_path(tmp_path.resolve(), value)
    assert project_name(" 中文 ") == "中文"
    assert version_key("V01.2") == version_key("1.2.0")
    for value in ["1.2-beta", "", " 1", "1.2.3.4.5.6"]:
        with pytest.raises(HubError):
            version_key(value)
