import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wildcard_sync import core, routes
from wildcard_sync.routes import TargetUnavailable


class Harness:
    def __init__(self, target: Path, token=None, refresh_error=None, unavailable=False):
        self.target = target
        self.token = token
        self.refresh_calls = 0
        self.refresh_error = refresh_error
        self.unavailable = unavailable

    def get_target_dir(self):
        if self.unavailable:
            raise TargetUnavailable("Impact Pack not loaded")
        return self.target

    def refresh(self):
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error

    def run(self, scenario):
        async def main():
            app = web.Application()
            app.add_routes(routes.build_routes(self.get_target_dir, self.refresh, lambda: self.token))
            async with TestClient(TestServer(app)) as client:
                return await scenario(client)

        return asyncio.run(main())


def body(*files, **extra):
    return {"files": [{"path": p, "content": c} for p, c in files], **extra}


@pytest.fixture
def target(tmp_path):
    return tmp_path / "custom_wildcards"


def test_put_then_hash_roundtrip(target):
    h = Harness(target)

    async def scenario(client):
        r = await client.put("/wildcard_sync", json=body(("a.txt", "red\n"), ("hair/short.txt", "bob\n")))
        assert r.status == 200
        put = await r.json()
        r = await client.get("/wildcard_sync/hash")
        assert r.status == 200
        return put, await r.json()

    put, got = h.run(scenario)
    assert put["files"] == 2 and put["refreshed"] is True
    assert got["hash"] == put["hash"] and got["files"] == 2
    assert (target / "hair" / "short.txt").read_text() == "bob\n"
    assert h.refresh_calls == 1


def test_put_replaces_previous_set(target):
    h = Harness(target)

    async def scenario(client):
        await client.put("/wildcard_sync", json=body(("old.txt", "x")))
        await client.put("/wildcard_sync", json=body(("new.txt", "y")))

    h.run(scenario)
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text() == "y"


@pytest.mark.parametrize(
    "payload",
    [
        body(("../escape.txt", "x")),
        body(("C:/Windows/evil.txt", "x")),
        body(("evil.py", "import os")),
        body(("a.yaml", "k: v")),
        {"files": "nope"},
    ],
)
def test_put_rejects_bad_payloads(target, payload):
    h = Harness(target)

    async def scenario(client):
        r = await client.put("/wildcard_sync", json=payload)
        return r.status, await r.json()

    status, data = h.run(scenario)
    assert status == 400 and "error" in data
    assert not target.exists()
    assert h.refresh_calls == 0


def test_put_rejects_non_json(target):
    async def scenario(client):
        r = await client.put("/wildcard_sync", data=b"\xff\xfe not json")
        return r.status

    assert Harness(target).run(scenario) == 400


def test_put_rejects_oversized_body(target, monkeypatch):
    monkeypatch.setattr(routes, "MAX_BODY_BYTES", 100)

    async def scenario(client):
        r = await client.put("/wildcard_sync", data=json.dumps(body(("a.txt", "x" * 500))))
        return r.status

    assert Harness(target).run(scenario) == 413
    assert not target.exists()


def test_token_enforced_when_configured(target):
    h = Harness(target, token="s3cret")

    async def scenario(client):
        statuses = []
        for headers in ({}, {"X-VP-Token": "wrong"}):
            statuses.append((await client.get("/wildcard_sync/hash", headers=headers)).status)
            statuses.append((await client.put("/wildcard_sync", json=body(("a.txt", "x")), headers=headers)).status)
        ok = await client.put("/wildcard_sync", json=body(("a.txt", "x")), headers={"X-VP-Token": "s3cret"})
        return statuses, ok.status

    statuses, ok = h.run(scenario)
    assert statuses == [401, 401, 401, 401]
    assert ok == 200


def test_open_when_no_token(target):
    async def scenario(client):
        return (await client.put("/wildcard_sync", json=body(("a.txt", "x")), headers={"X-VP-Token": "anything"})).status

    assert Harness(target, token=None).run(scenario) == 200


def test_target_unavailable_returns_503(target):
    async def scenario(client):
        return (await client.get("/wildcard_sync/hash")).status, (await client.put("/wildcard_sync", json=body())).status

    assert Harness(target, unavailable=True).run(scenario) == (503, 503)


def test_refresh_failure_still_reports_written(target):
    h = Harness(target, refresh_error=RuntimeError("boom"))

    async def scenario(client):
        r = await client.put("/wildcard_sync", json=body(("a.txt", "x")))
        return r.status, await r.json()

    status, data = h.run(scenario)
    assert status == 200 and data["refreshed"] is False
    assert (target / "a.txt").exists()


def test_hash_mismatch_rejected(target):
    async def scenario(client):
        r = await client.put("/wildcard_sync", json=body(("a.txt", "x"), hash="0" * 64))
        return r.status

    assert Harness(target).run(scenario) == 400
