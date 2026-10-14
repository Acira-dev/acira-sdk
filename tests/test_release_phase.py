"""Phase evidence must observe the complete pytest process and exact package bytes."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("release_phase", Path(__file__).resolve().parents[1] / "tools/run_release_phase.py")
phase = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase)


def package_tree(root):
    root.mkdir()
    for name in phase.PACKAGES:
        directory = root / name
        directory.mkdir()
        (directory / "__init__.py").write_text("# inert test package\n")
    return phase.package_files(root)


def wheel_file(path, files, *, bad_hash=False, duplicate_record=False):
    entries = {name: b"# inert test package\n" for name in files}
    entries["acira-0.1.1.dist-info/METADATA"] = b"Name: acira\nVersion: 0.1.1\n"
    rows = []
    for name, data in entries.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append([name, "sha256=" + ("bad" if bad_hash and name.endswith("METADATA") else encoded), str(len(data))])
    if duplicate_record:
        rows.append(rows[0])
    record = "acira-0.1.1.dist-info/RECORD"
    rows.append([record, "", ""])
    stream = io.StringIO()
    csv.writer(stream).writerows(rows)
    with zipfile.ZipFile(path, "w") as output:
        for name, data in entries.items():
            output.writestr(name, data)
        output.writestr(record, stream.getvalue())


def ready(tmp_path, monkeypatch, *, installed=False):
    root = tmp_path / "source"
    files = package_tree(root)
    (root / "tests").mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    phase.save(inputs / "source-manifest.json", files)
    wheel = tmp_path / "acira.whl" if installed else None
    if installed:
        wheel_file(wheel, files)
    identity = dict(source_sha=None, run_id=None, run_attempt=None, job_id=None,
                    job_name=None, wheel_sha256=phase.digest(wheel) if wheel else None)
    phase.save(inputs / "identity.json", identity)
    inert_inventory = [{"name": "acira", "version": "0.1.1"}]
    phase.save(inputs / "inventory.json", inert_inventory)
    monkeypatch.setattr(phase, "inventory", lambda: inert_inventory)
    directory = tmp_path / "result"
    directory.mkdir()
    output = directory / "phase.json"
    monkeypatch.chdir(tmp_path if installed else root)
    monkeypatch.setattr(sys, "path", ["", *sys.path[1:]])
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    if installed:
        monkeypatch.setenv("ACIRA_TEST_INSTALLED", "1")
    args = ["-q"] + ([str(root / "tests"), "--import-mode=importlib"] if installed else [])
    args += ["--basetemp=" + str(directory / "pytest-temp")]
    return root, inputs, wheel, output, args


def fake_pytest(monkeypatch, package_root, *, status=0, skipped=False, deselected=False, callback=None):
    calls = []
    def main(args, plugins):
        calls.append(list(args))
        witness, = plugins
        options = SimpleNamespace(keyword="", markexpr="", collectonly=False)
        witness.pytest_configure(SimpleNamespace(invocation_params=SimpleNamespace(args=tuple(args)),
                                 getini=lambda name: [], option=options))
        for name in phase.PACKAGES:
            monkeypatch.setitem(sys.modules, name, SimpleNamespace(__file__=str(package_root / name / "__init__.py")))
        session = SimpleNamespace(items=[SimpleNamespace(nodeid="tests/test_inert.py::test_one")])
        witness.pytest_collection_finish(session)
        if deselected:
            witness.pytest_deselected([object()])
        witness.pytest_runtest_logreport(SimpleNamespace(when="call", passed=not skipped,
                                        failed=False, skipped=skipped))
        if callback:
            callback()
        witness.pytest_sessionfinish(session, status)
        return status
    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=main))
    return calls


def test_source_phase_observes_one_public_main_and_exact_final_origins(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    calls = fake_pytest(monkeypatch, root)
    assert phase.run(root, inputs, "source", wheel, output, args) == 0
    result = json.loads(output.read_text())
    assert calls == [args] and result["complete"] is True
    assert result["collection"]["count"] == result["outcomes"]["passed"] == 1
    assert result["origins"]["finish"] == {name: str(root / name / "__init__.py") for name in phase.PACKAGES}
    assert result["source_manifest"]["before_sha256"] == result["source_manifest"]["after_sha256"]
    nodeids = json.loads(output.with_suffix(".nodeids.json").read_text())
    assert result["collection"]["nodeids_sha256"] == hashlib.sha256(phase.canonical(nodeids)).hexdigest()


def test_installed_phase_checks_entire_wheel_record_and_actual_package_tree(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch, installed=True)
    installed = tmp_path / "site-packages"
    package_tree(installed)
    calls = fake_pytest(monkeypatch, installed)
    assert phase.run(root, inputs, "installed-wheel", wheel, output, args) == 0
    result = json.loads(output.read_text())
    assert calls == [args] and result["wheel"]["record_verified"] is True
    assert result["wheel"]["sha256"] == phase.digest(wheel)


@pytest.mark.parametrize("skipped,deselected", [(True, False), (False, True)])
def test_skip_or_deselection_refuses_completion_without_changing_pytest_status(tmp_path, monkeypatch, skipped, deselected):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    fake_pytest(monkeypatch, root, skipped=skipped, deselected=deselected)
    assert phase.run(root, inputs, "source", wheel, output, args) == 1
    result = json.loads(output.read_text())
    assert result["complete"] is False and result["outcomes"]["exitstatus"] == 0


@pytest.mark.parametrize("change", ["source-bytes", "missing-package", "extra-package-file"])
def test_unknown_or_changed_final_provenance_is_not_a_pass(tmp_path, monkeypatch, change):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    def mutate():
        if change == "source-bytes":
            (root / "acira/__init__.py").write_text("# changed\n")
        elif change == "missing-package":
            monkeypatch.delitem(sys.modules, "acira_dashboard")
        else:
            (root / "acira/untracked.py").write_text("# extra\n")
    fake_pytest(monkeypatch, root, callback=mutate)
    assert phase.run(root, inputs, "source", wheel, output, args) == 1
    assert json.loads(output.read_text())["complete"] is False


def test_actual_pytest_nonzero_status_is_preserved(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    fake_pytest(monkeypatch, root, status=3)
    assert phase.run(root, inputs, "source", wheel, output, args) == 3
    assert json.loads(output.read_text())["outcomes"]["exitstatus"] == 3


@pytest.mark.parametrize("bad_hash,duplicate", [(True, False), (False, True)])
def test_bad_metadata_record_or_duplicate_row_is_rejected(tmp_path, bad_hash, duplicate):
    root = tmp_path / "source"
    files = package_tree(root)
    wheel = tmp_path / "bad.whl"
    wheel_file(wheel, files, bad_hash=bad_hash, duplicate_record=duplicate)
    with pytest.raises(ValueError):
        phase.wheel_packages(wheel)


@pytest.mark.parametrize("change", ["filtered-args", "autoload-disabled", "existing-output", "wrong-source-before"])
def test_phase_contract_refuses_before_pytest(tmp_path, monkeypatch, change):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    calls = fake_pytest(monkeypatch, root)
    if change == "filtered-args":
        args += ["-k", "one"]
    elif change == "autoload-disabled":
        monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    elif change == "existing-output":
        output.write_text("{}")
    else:
        (root / "acira/__init__.py").write_text("# changed before\n")
    with pytest.raises(ValueError):
        phase.run(root, inputs, "source", wheel, output, args)
    assert calls == []


def test_source_manifest_rejects_parent_paths_and_duplicate_entries(tmp_path):
    root = tmp_path / "source"
    package_tree(root)
    for names in (["../outside"], ["acira/__init__.py", "acira/__init__.py"]):
        with pytest.raises(ValueError):
            phase.source_files(root, names)


def test_first_pytest_interruption_survives_receipt_failure(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    original = KeyboardInterrupt()
    def main(*args, **kwargs):
        raise original
    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=main))
    monkeypatch.setattr(phase, "save", lambda *args: (_ for _ in ()).throw(OSError("inert write failure")))
    with pytest.raises(KeyboardInterrupt) as caught:
        phase.run(root, inputs, "source", wheel, output, args)
    assert caught.value is original


def test_first_pytest_interruption_survives_final_origin_observation_failure(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    original = KeyboardInterrupt()
    def main(*args, **kwargs):
        raise original
    def failed_origins():
        raise OSError("inert final observation failure")
    monkeypatch.setitem(sys.modules, "pytest", SimpleNamespace(main=main))
    monkeypatch.setattr(phase, "origins", failed_origins)
    with pytest.raises(KeyboardInterrupt) as caught:
        phase.run(root, inputs, "source", wheel, output, args)
    assert caught.value is original
    result = json.loads(output.read_text())
    assert result["complete"] is False and result["outcomes"]["exitstatus"] is None
    assert result["origins"]["finish"] == dict.fromkeys(phase.PACKAGES)
    assert result["verification_errors"] == ["pytest-main-raised:KeyboardInterrupt", "phase-provenance:OSError"]


def test_nonzero_pytest_exit_survives_later_final_origin_interruption(tmp_path, monkeypatch):
    root, inputs, wheel, output, args = ready(tmp_path, monkeypatch)
    fake_pytest(monkeypatch, root, status=3)
    actual_origins = phase.origins
    calls = []
    def observe():
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt()
        return actual_origins()
    monkeypatch.setattr(phase, "origins", observe)
    assert phase.run(root, inputs, "source", wheel, output, args) == 3
    result = json.loads(output.read_text())
    assert calls == [1, 1, 1]
    assert result["complete"] is False and result["outcomes"]["exitstatus"] == 3
    assert result["verification_errors"] == ["phase-provenance:KeyboardInterrupt"]
