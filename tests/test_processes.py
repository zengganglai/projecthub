import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from projecthub.application import AppConfig, HttpConfig, resolve_settings
from projecthub.contracts import HubError
from projecthub.runtime import service_status, start_service, stop_service
from projecthub.storage import RuntimeLock
from scripts.migrate import main as migrate
from scripts.sync_files import main as sync_files

_CODE = Path(__file__).resolve().parents[1]


def test_runtime_lock_excludes_other_processes_and_releases_on_close(tmp_path):
    root = tmp_path / "project space"
    settings = resolve_settings(AppConfig(project_root=root))
    child = """
from pathlib import Path
from sys import argv
from projecthub.application import AppConfig, resolve_settings
from projecthub.contracts import HubError
from projecthub.storage import RuntimeLock
lock = RuntimeLock(resolve_settings(AppConfig(project_root=Path(argv[1]))))
try:
    lock.acquire()
except HubError as error:
    print(error.code)
else:
    lock.release()
    print("acquired")
"""

    def attempt():
        return subprocess.check_output(
            [sys.executable, "-B", "-c", child, str(root)], cwd=_CODE, text=True, timeout=15
        ).strip()

    lock = RuntimeLock(settings)
    lock.acquire()
    try:
        lock.acquire()
        assert attempt() == "SERVICE_RUNNING"
    finally:
        lock.release()
    lock.release()
    assert attempt() == "acquired"


def test_maintenance_checks_runtime_lock_and_reports_invalid_metadata(tmp_path, capsys):
    settings = resolve_settings(AppConfig(project_root=tmp_path / "project space"))
    lock = RuntimeLock(settings)
    lock.acquire()
    try:
        assert migrate(["--project-root", str(settings.project_root)]) == 1
        assert "SERVICE_RUNNING" in capsys.readouterr().err
        assert not settings.database.path.exists()
    finally:
        lock.release()

    source = tmp_path / "metadata.json"
    source.write_text('{"a": {}, "a": {}}', encoding="utf-8")
    assert sync_files(["--project-root", str(settings.project_root), "--metadata", str(source)]) == 1
    assert "MAINTENANCE_FAILED" in capsys.readouterr().err
    assert not settings.database.path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows managed-process contract")
def test_stop_refuses_a_pid_with_inconsistent_process_identity(tmp_path):
    settings = resolve_settings(AppConfig(project_root=tmp_path))
    logs = settings.storage.logs_dir
    logs.mkdir(parents=True)
    path = logs / "service.json"
    record = {
        "pid": os.getpid(),
        "start_time": 1,
        "command_line": "unrelated",
        "interpreter": sys.executable,
        "project_root": str(settings.project_root),
        "code_root": str(_CODE),
        "nonce": "1" * 32,
        "ready": True,
        "port": 8001,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(HubError) as caught:
        stop_service(settings)
    assert caught.value.code == "SERVICE_IDENTITY_MISMATCH"
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert not (logs / "service-stop.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows managed-process contract")
def test_managed_service_is_ready_and_stops_only_its_project(tmp_path):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    settings = AppConfig(
        project_root=tmp_path / "project space", http=HttpConfig(bind_host="127.0.0.1", port=port)
    )
    unrelated = replace(settings, project_root=tmp_path / "unrelated project")
    try:
        status = start_service(settings, timeout=12)
        assert status["running"] and status["ready"] and status["port"] == port
        record_path = resolve_settings(settings).storage.logs_dir / "service.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["project_root"] == str(settings.project_root.resolve())
        assert record["code_root"] == str(_CODE)
        assert service_status(settings) == status
        assert stop_service(unrelated) == {"running": False, "ready": False}
        assert service_status(settings) == status
        with pytest.raises(HubError) as duplicate:
            start_service(settings, timeout=12)
        assert duplicate.value.code == "SERVICE_ALREADY_RUNNING"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as probe:
            probe.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            assert probe.recv(4096).startswith(b"HTTP/1.1 200")
    finally:
        if service_status(settings)["running"]:
            stop_service(settings, timeout=12)
    assert service_status(settings) == {"running": False, "ready": False}
    logs = resolve_settings(settings).storage.logs_dir
    assert not (logs / "service.json").exists()
    assert not (logs / "service-stop.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows managed-process contract")
def test_managed_bind_failure_removes_its_run_record(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        settings = AppConfig(
            project_root=tmp_path / "project with occupied port",
            http=HttpConfig(bind_host="127.0.0.1", port=port),
        )
        with pytest.raises(HubError) as failed:
            start_service(settings, timeout=12)
        assert failed.value.code == "SERVICE_START_FAILED"
    assert service_status(settings) == {"running": False, "ready": False}
    logs = resolve_settings(settings).storage.logs_dir
    assert not (logs / "service.json").exists()
