import asyncio
import importlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from acira.project import initialize
from acira.local import atomic_json
from acira.retention import maintain
from acira.adapters.browser_use_operations import NetworkGate
from acira.adapters._submission_contract import RequestContract
from acira.adapters import discover_adapter_ids


def test_discovered_builtin_adapters_satisfy_the_public_factory_contract():
    adapters = discover_adapter_ids()
    assert adapters
    for adapter in adapters:
        module = importlib.import_module("acira.adapters." + adapter)
        assert callable(getattr(module, "create", None)), f"Helper module advertised as an adapter: {adapter}"


def test_legacy_archives_share_project_quota_and_active_archives_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[])
    document = project.document()
    document["retention"] = {"audit_bytes": 4*1024*1024, "audit_days": 1}
    atomic_json(project.path, document)
    root = project.root / ".acira/audits"
    closed = root / ("a"*32)
    active = root / ("b"*32)
    closed.mkdir(parents=True)
    active.mkdir()
    (closed / "session.json").write_text(json.dumps({"session_id": closed.name, "ended_at": "2026-01-01", "end_reason": "completed"}))
    (closed / "events.jsonl.gz").write_bytes(b"x" * 1024*1024)
    (root / (closed.name+".zip")).write_bytes(b"x"*1024*1024)
    (active / "events.jsonl").write_bytes(b"x"*1024)
    for path in [closed, *closed.iterdir()]:
        os.utime(path, (time.time()-3*86400, time.time()-3*86400))
    report = maintain(project)
    assert report["pruned_archives"] == 1
    assert not closed.exists()
    assert (active / "events.jsonl").exists()
    assert report["archives_bytes"] + report["journal_bytes"] <= report["audit_bytes_limit"]
    (active / "events.jsonl").write_bytes(b"x"*4*1024*1024)
    with pytest.raises(OSError, match="Active or unfinalized"):
        maintain(project)
    assert active.exists()


def test_project_persistent_archive_path_is_anchored_to_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[])
    document = project.document()
    document["agents"]["default"]["guard"] = {"audit": {"mode": "persistent", "directory": "custom-audits"}}
    atomic_json(project.path, document)
    monkeypatch.chdir(tmp_path)
    assert project.snapshot().config.audit.directory == str(project.root / "custom-audits")


@pytest.mark.asyncio
async def test_request_audit_failure_cancels_intercepted_request():
    sent = []
    writes = []
    class FakeChannel:
        handlers = {}
        global_handlers = {}
        target_id = "page"
        async def send(self, method, params=None):
            sent.append((method, params))
    class Store:
        def event(self, *args, **kwargs):
            writes.append(args[0])
            raise OSError("Audit disk is full")
    channel = FakeChannel()
    policy = SimpleNamespace(before_decision=None, policy_revision="r1")
    manager = SimpleNamespace(guard_policy=policy, action_gateway=SimpleNamespace(checkpoint_store=Store()))
    gate = NetworkGate(manager, channel)
    contract = RequestContract(manager, channel, "https://test.invalid/payment", {"amount": "10"}, "json", "POST", {}, 1)
    contract.authorized_revision = "r1"
    contract.frame_id = "frame"
    contract.redirect_token = object()  # execute() assigns the private attempt identity.
    gate.active = contract
    gate.locks.add(contract.destination)
    await gate.paused({"requestId": "private", "networkId": "network", "frameId": "frame", "resourceType": "Fetch", "request": {
        "url": contract.destination, "method": "POST", "headers": {"Content-Type": "application/json"}, "postData": '{"amount":"10"}'}})
    assert [method for method, _ in sent] == ["Fetch.failRequest"]
    assert writes == ["submission_dispatch_attempt"]
    assert (await contract.outcome)["blocked"] is True


def test_request_payload_and_review_labels_are_bounded():
    with pytest.raises(ValueError, match="64 KiB"):
        RequestContract(None, None, "https://test.invalid/pay", {"value":"x"*65537}, "json", "POST", {}, 1)
