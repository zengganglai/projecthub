import asyncio
from dataclasses import replace

import httpx
import pytest

from projecthub.application import AppConfig, HttpConfig, create_app, run
from projecthub.contracts import HubError
from projecthub.database import Database
from projecthub.storage import RuntimeLock


def test_run_reports_failed_lifespan(tmp_path, monkeypatch):
    def fail_open(self, **kwargs):
        raise HubError("INJECTED_STARTUP_FAILURE")

    monkeypatch.setattr(Database, "open", fail_open)
    with pytest.raises(SystemExit) as failure:
        run(AppConfig(project_root=tmp_path))
    assert failure.value.code != 0


def test_shutdown_drains_responses_and_finishes_after_waiter_cancel(tmp_path):
    async def scenario():
        app = create_app(AppConfig(project_root=tmp_path))
        entered, release = asyncio.Event(), asyncio.Event()

        @app.get("/hold")
        async def hold():
            entered.set()
            await release.wait()
            return {"done": True}

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                request = asyncio.create_task(client.get("/hold"))
                await asyncio.wait_for(entered.wait(), 3)
                closing = asyncio.create_task(app.state.close())
                while app.state.ready:
                    await asyncio.sleep(0)
                assert not closing.done()
                assert (await client.get("/hold")).status_code == 503
                assert (await client.get("/health/ready")).status_code == 503
                closing.cancel()
                release.set()
                assert (await request).status_code == 200
                with pytest.raises(asyncio.CancelledError):
                    await closing
                assert app.state.resources == []
                assert app.state.active_requests == 0

    asyncio.run(scenario())


def test_close_failure_retains_database_and_lock_until_retry(tmp_path):
    async def scenario():
        app = create_app(AppConfig(project_root=tmp_path))
        async with app.router.lifespan_context(app):
            original = app.state.files.close

            def fail_close():
                raise OSError("injected close failure")

            app.state.files.close = fail_close
            with pytest.raises(HubError) as failure:
                await app.state.close()
            assert failure.value.code == "APPLICATION_CLOSE_FAILED"
            assert {item[0] for item in app.state.resources} == {"files", "database", "lock"}
            assert app.state.db.one("SELECT count(*) AS n FROM users")["n"] >= 1
            contender = RuntimeLock(app.state.settings)
            with pytest.raises(HubError) as failure:
                await asyncio.to_thread(contender.acquire)
            assert failure.value.code == "SERVICE_RUNNING"
            app.state.files.close = original
            await app.state.close()
            assert app.state.resources == []
            await asyncio.to_thread(contender.acquire)
            await asyncio.to_thread(contender.release)

    asyncio.run(scenario())


def test_concurrency_limit_releases_slots_and_exempts_probes(tmp_path):
    async def scenario():
        app = create_app(
            AppConfig(project_root=tmp_path, http=replace(HttpConfig(), max_concurrent_requests=8))
        )
        entered, release = asyncio.Queue(), asyncio.Event()

        @app.get("/hold")
        async def hold():
            entered.put_nowait(None)
            await release.wait()
            return {"done": True}

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                requests = [asyncio.create_task(client.get("/hold")) for _ in range(8)]
                try:
                    for _ in requests:
                        await asyncio.wait_for(entered.get(), 3)
                    response = await client.get("/hold")
                    assert response.status_code == 503
                    assert response.json()["data"]["error"]["code"] == "SERVER_BUSY"
                    assert (await client.get("/health/ready")).status_code == 200
                finally:
                    release.set()
                    results = await asyncio.gather(*requests)
                assert all(response.status_code == 200 for response in results)
                assert (await client.get("/hold")).status_code == 200
                assert app.state.active_requests == 0

    asyncio.run(scenario())
