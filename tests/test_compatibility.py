"""Compare every HTTP step, asserting exact permission and curve-name differences."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import rfc8785

_SCENARIO = Path(__file__).with_name("compatibility_scenario.py")
_REFERENCE_ROOT = Path(os.getenv("PROJECTHUB_REFERENCE_ROOT", "Z:/Code/DeviceBridge"))
_PROJECT_ROOT = _SCENARIO.parents[1]
_LEGACY_GLOBAL_PERMISSIONS = ["curve.read", "factory.debug", "firmware.read", "internal.read", "public.read"]
_LEGACY_DEFAULT_PERMISSIONS = ["firmware.read", "public.read"]
_DESCRIPTION_CHANGES = {
    "firmware.read": (
        "所有已登录用户默认可读取固件列表、JSON、检查升级、下载及 HEAD 信息",
        "读取本型号全部版本的固件列表、JSON、检查升级、下载及 HEAD 信息",
    ),
    "public.read": (
        "所有已登录用户默认可读取公共文件列表、JSON、原始文件及 HEAD 信息",
        "读取本型号全部版本的公共文件列表、JSON、原始文件及 HEAD 信息",
    ),
    "public.manage": (
        "直接上传公共文件，备份并直接删除；所有已登录用户默认可读取",
        "直接上传、备份及删除本型号的公共文件；读取需单独授权",
    ),
}


def _run(implementation, source, temporary_root):
    # Import just one service; neither source checkout's data directory is used.
    assert not temporary_root.exists()
    assert not temporary_root.resolve().is_relative_to(source.resolve())
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(source),
    }
    result = subprocess.run(
        [sys.executable, "-B", str(_SCENARIO), implementation, str(temporary_root)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_public_api_matches_isolated_devicebridge():
    if not (_REFERENCE_ROOT / "device_bridge" / "application").is_dir():
        pytest.skip("DeviceBridge reference checkout unavailable; set PROJECTHUB_REFERENCE_ROOT.")
    # Both implementations always receive fresh local roots, including when pytest
    # uses a mapped SMB workspace for tmp_path. Nothing is copied from source data.
    with TemporaryDirectory(prefix="projecthub-compat-") as directory:
        root = Path(directory)
        assert not root.resolve().drive.startswith("\\\\"), "Compatibility roots must be on local storage"
        _compare(root)


def _assert_permission_changes(expected, actual):
    """Accept only 24 exact field changes across 11 named responses, after checking both values."""
    changed_paths = set()

    def changed(step, path, previous, current):
        assert previous != current, (step, path, "Not an intentional difference")
        location = (step, path)
        assert location not in changed_paths, location
        reference, observed = expected[step], actual[step]
        for key in path[:-1]:
            reference, observed = reference[key], observed[key]
        key = path[-1]
        assert reference[key] == previous, (step, path, "reference", reference[key], previous)
        assert observed[key] == current, (step, path, "projecthub", observed[key], current)
        reference[key] = current
        changed_paths.add(location)

    changed("admin-identity", ("data", "global_permissions"), _LEGACY_GLOBAL_PERMISSIONS, [])
    for step in ("create-user", "update-user", "read-user-defaults", "read-granted-identity"):
        changed(step, ("data", "global_permissions"), _LEGACY_DEFAULT_PERMISSIONS, [])
    assert [user["is_admin"] for user in expected["list-users"]["data"]["users"]] == [True, False, False]
    changed("list-users", ("data", "users", 0, "global_permissions"), _LEGACY_GLOBAL_PERMISSIONS, [])
    changed("list-users", ("data", "users", 1, "global_permissions"), _LEGACY_DEFAULT_PERMISSIONS, [])
    changed("list-users", ("data", "users", 2, "global_permissions"), _LEGACY_DEFAULT_PERMISSIONS, [])
    for step in (
        "read-empty-permissions",
        "grant-permissions",
        "read-granted-permissions",
        "renamed-permissions",
        "replace-without-project-grants",
    ):
        assert expected[step]["data"]["global_permissions"] == []
        assert actual[step]["data"]["global_permissions"] == []

    reference_catalog = expected["permission-directory"]["data"]["permissions"]
    actual_catalog = actual["permission-directory"]["data"]["permissions"]
    assert len(reference_catalog) == len(actual_catalog) == 14
    assert [item["code"] for item in reference_catalog] == [item["code"] for item in actual_catalog]
    for index, permission in enumerate(reference_catalog):
        code = permission["code"]
        path = ("data", "permissions", index)
        if code in _LEGACY_GLOBAL_PERMISSIONS:
            changed("permission-directory", (*path, "scopes"), ["global", "project"], ["project"])
        else:
            assert permission["scopes"] == actual_catalog[index]["scopes"] == ["project"]
        if code in _DESCRIPTION_CHANGES:
            previous, current = _DESCRIPTION_CHANGES[code]
            changed("permission-directory", (*path, "description"), previous, current)

    hidden = {"error": {"code": "PROJECT_NOT_FOUND"}}
    for step in ("curve-read-forbidden", "revoked-curve-access"):
        changed(step, ("status",), 403, 404)
        changed(step, ("data",), {"error": {"code": "PERMISSION_DENIED"}}, hidden)
    changed("default-public-read", ("status",), 200, 404)
    changed("default-public-read", ("data",), {"files": [], "count": 0}, hidden)

    # The reference still exposes exactly the previously listed public file after
    # revocation, with only the already-tested model/version rename reflected.
    public_list = expected["public-list"]["data"]
    assert public_list.keys() == {"files", "count"}
    assert public_list["count"] == len(public_list["files"]) == 1
    public_file = public_list["files"][0]
    assert public_file["project"] == "M910" and public_file["project_version"] == "初版"
    assert public_file["path"] == "M910/初版/public/public.json"
    renamed_public = {
        "files": [
            {
                **public_file,
                "project": "M920",
                "project_version": "新版",
                "path": "M920/新版/public/public.json",
            }
        ],
        "count": 1,
    }
    changed("default-access-persists", ("status",), 200, 404)
    changed("default-access-persists", ("data",), renamed_public, hidden)
    assert len(changed_paths) == 24
    assert len({step for step, _ in changed_paths}) == 11


def _assert_curve_name_changes(expected, actual):
    """Accept 15 added names and four exact revision changes across 14 responses."""
    changed_paths = set()

    def containers(step, path):
        location = (step, path)
        assert location not in changed_paths, location
        reference, observed = expected[step], actual[step]
        for key in path[:-1]:
            reference, observed = reference[key], observed[key]
        changed_paths.add(location)
        return reference, observed, path[-1]

    def added_name(step, path, name):
        reference, observed, key = containers(step, (*path, "curve_name"))
        assert key not in reference, (step, path, "Reference must not expose curve_name")
        assert observed[key] == name, (step, path, observed[key], name)
        reference[key] = name

    fields_path = ("data", "field_info", "fields")
    for step in (
        "submit-curve-a",
        "review-detail",
        "submit-curve-update",
        "renamed-pending-review",
        "request-curve-deletion",
        "request-curve-deletion-again",
    ):
        added_name(step, fields_path, "曲线A")
    added_name("review-list", ("data", "reviews", 0, "field_info", "fields"), "曲线A")
    added_name("submit-curve-b", fields_path, "曲线B")
    added_name("submit-rejected-curve", fields_path, "驳回曲线")
    added_name("approve-curve-a", ("data",), "曲线A")
    added_name("approve-curve-b", ("data",), "曲线B")
    # The update request omits curve_name, so approval and deletion must retain A.
    added_name("approve-curve-update", ("data",), "曲线A")
    added_name("filter-by-literal", ("data", "files", 0), "曲线A")
    added_name("sorted-curves", ("data", "files", 0), "曲线B")
    added_name("sorted-curves", ("data", "files", 1), "曲线A")
    assert len(changed_paths) == 15

    def revisions(raw, curve_type, description):
        # Independently derive both contracts from the known file bytes and fields;
        # never accept an arbitrary digest or derive it from the observed response.
        fields = {
            "id": 1,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "curve_type": curve_type,
            "description": description,
        }
        previous = hashlib.sha256(rfc8785.dumps(fields)).hexdigest()
        current = hashlib.sha256(rfc8785.dumps({**fields, "curve_name": "曲线A"})).hexdigest()
        assert previous != current
        return previous, current

    original = revisions('{"名称": "一号曲线", "值":[1,2]}\n'.encode(), "工况", "匹配%_")
    updated = revisions(b'{"name": "edited"}\n', "已调整", "新的描述")
    for step, (previous, current) in (
        ("submit-curve-update", original),
        ("renamed-pending-review", original),
        ("request-curve-deletion", updated),
        ("request-curve-deletion-again", updated),
    ):
        path = ("data", "field_info", "base_revision")
        reference, observed, key = containers(step, path)
        assert reference[key] == previous, (step, path, "reference", reference[key], previous)
        assert observed[key] == current, (step, path, "projecthub", observed[key], current)
        reference[key] = current
    assert len(changed_paths) == 19
    assert len({step for step, _ in changed_paths}) == 14


def _compare(root):
    expected = _run("reference", _REFERENCE_ROOT, root / "reference")
    actual = _run("projecthub", _PROJECT_ROOT, root / "projecthub")
    assert actual.keys() == expected.keys()
    # Original 115 requests are retained; granted identity adds one contract step.
    assert len(expected) == 116
    _assert_permission_changes(expected, actual)
    _assert_curve_name_changes(expected, actual)
    mismatches = {step: (expected[step], actual[step]) for step in expected if expected[step] != actual[step]}
    assert mismatches == {}
