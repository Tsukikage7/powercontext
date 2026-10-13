# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The public installer must not leave mixed versions after a rejected upgrade."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from powercontext.cli import authorization, opencode
from powercontext.cli.guidance import HOST_GUIDANCE, RESOURCE_MANIFEST
from powercontext.cli.system import SetupError


def _package(root: Path, version: str) -> Path:
    package = root / version
    (package / "lib").mkdir(parents=True)
    (package / "package.json").write_text('{"name":"powercontext-opencode"}', encoding="utf-8")
    for name in ("index.js", "tui.js"):
        (package / "lib" / name).write_text(f"export default {{version: '{version}'}};\n", encoding="utf-8")
    shutil.copytree(
        Path(__file__).parents[1] / HOST_GUIDANCE["opencode"].skill, package / opencode.OPENCODE_SKILL.parent
    )
    entry = package / opencode.OPENCODE_SKILL
    entry.write_text(
        entry.read_text(encoding="utf-8").replace("description: ", f"description: {version} "), encoding="utf-8"
    )
    return package


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture(params=[False, True], ids=["fresh", "upgrade"])
def installation(request, tmp_path: Path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "unrelated.json").write_bytes(b'{"keep": true}\r\n')
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config))
    monkeypatch.setattr(opencode, "opencode_executable", lambda: "opencode")
    monkeypatch.setattr(opencode, "_version", lambda: "1.18.21")
    monkeypatch.setattr(opencode, "opencode_config_dir", lambda: config)
    monkeypatch.setattr(authorization, "setup_authorization_value", lambda _host: None)
    if request.param:
        old = _package(tmp_path, "old")
        opencode.install_opencode_plugin(source=str(old), ref="unused")
        skill = config / "skills/powercontext-project-context"
        (skill / "personal.md").write_bytes(b"My personal instructions\r\n")
        entry = skill / "SKILL.md"
        entry.write_bytes(entry.read_bytes() + b"\r\nMy appended notes\r\n")
        (config / "plugins/powercontext-opencode-tui.js").write_bytes(b"old legacy plugin\n")
    tui = config / "tui.json"
    payload = json.loads(tui.read_text(encoding="utf-8")) if tui.exists() else {}
    payload["theme"] = "user-theme"
    payload.setdefault("plugin", []).append("user-plugin")
    tui.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return config, _package(tmp_path, "new"), request.param


def _assert_success(config: Path, package: Path, upgrade: bool) -> None:
    assert (config / "plugins/powercontext-opencode.js").read_bytes() == (package / "lib/index.js").read_bytes()
    tui = json.loads((config / "tui.json").read_text(encoding="utf-8"))
    assert str((package / "lib/tui.js").resolve()) in tui["plugin"]
    assert "user-plugin" in tui["plugin"]
    assert tui["theme"] == "user-theme"
    skill = config / "skills/powercontext-project-context"
    assert "description: new " in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert not (config / "plugins/powercontext-opencode-tui.js").exists()
    if upgrade:
        assert (skill / "personal.md").read_bytes() == b"My personal instructions\r\n"
        assert (skill / "SKILL.md").read_bytes().endswith(b"\r\nMy appended notes\r\n")
    assert (config / "unrelated.json").read_bytes() == b'{"keep": true}\r\n'
    assert not list(config.rglob("*.tmp"))
    assert not list(config.glob(".powercontext-install-*"))
    assert not list(skill.parent.glob(f".{skill.name}.*.bak"))


@pytest.mark.parametrize("invalid", ["missing-reference", "invalid-manifest"])
def test_rejected_package_preserves_whole_installation_and_can_retry(installation, invalid: str) -> None:
    config, package, upgrade = installation
    before = _snapshot(config)
    source = package / opencode.OPENCODE_SKILL.parent
    path = source / ("references/work-handoff.md" if invalid == "missing-reference" else RESOURCE_MANIFEST)
    original = path.read_bytes()
    if invalid == "missing-reference":
        path.unlink()
    else:
        path.write_bytes(b"[]")
    with pytest.raises(SetupError):
        opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert _snapshot(config) == before
    path.write_bytes(original)
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    _assert_success(config, package, upgrade)
    successful = _snapshot(config)
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert _snapshot(config) == successful


@pytest.mark.parametrize(
    "destination",
    [
        "plugins/.powercontext-opencode.json",
        "plugins/powercontext-opencode.js",
        "tui.json",
        "skills/powercontext-project-context",
    ],
)
def test_commit_failure_preserves_whole_installation_and_can_retry(installation, destination: str, monkeypatch) -> None:
    config, package, upgrade = installation
    before = _snapshot(config)
    real_replace = os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if Path(target) == config / destination and not failed:
            failed = True
            message = "injected installation write failure"
            raise OSError(message)
        return real_replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_once)
        with pytest.raises(SetupError):
            opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert failed
    assert _snapshot(config) == before
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    _assert_success(config, package, upgrade)


@pytest.mark.parametrize("installation", [True], indirect=True)
@pytest.mark.parametrize(
    "destination",
    [
        "plugins/.powercontext-opencode.json",
        "plugins/powercontext-opencode.js",
        "tui.json",
        "plugins/powercontext-opencode-tui.js",
    ],
)
def test_failed_file_recovery_blocks_retry_until_restored(installation, destination: str, monkeypatch) -> None:
    config, package, _upgrade = installation
    before = _snapshot(config)
    target = config / destination
    old_content = target.read_bytes()
    real_replace = os.replace
    commit_failed = False

    def fail_commit_and_recovery(source, target):
        nonlocal commit_failed
        if Path(target) == config / "skills/powercontext-project-context" and not commit_failed:
            commit_failed = True
            raise OSError("commit")
        if commit_failed and Path(target) == config / destination:
            raise OSError("recovery")
        return real_replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_commit_and_recovery)
        with pytest.raises(SetupError, match=r"recovery failed.*backups retained") as failure:
            opencode.install_opencode_plugin(source=str(package), ref="unused")
    backups = list(config.glob(".powercontext-install-*"))
    assert len(backups) == 1
    assert target.name in str(failure.value)
    assert str(backups[0]) in str(failure.value)
    old_file = next(path for path in backups[0].iterdir() if path.is_file() and path.read_bytes() == old_content)
    pending = _snapshot(config)
    with pytest.raises(SetupError, match="recovery") as retry:
        opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert str(backups[0]) in str(retry.value)
    assert _snapshot(config) == pending

    # Simulate the operator restoring the reported backup before trying again.
    os.replace(old_file, target)
    shutil.rmtree(backups[0])
    assert _snapshot(config) == before
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    _assert_success(config, package, True)
    successful = _snapshot(config)
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert _snapshot(config) == successful


@pytest.mark.parametrize("installation", [True], indirect=True)
@pytest.mark.parametrize("also_fail_plugin_recovery", [False, True], ids=["skill", "skill-and-plugin"])
def test_failed_skill_recovery_reports_backup_and_blocks_retry(
    installation, also_fail_plugin_recovery: bool, monkeypatch
) -> None:
    config, package, _upgrade = installation
    before = _snapshot(config)
    skill = config / "skills/powercontext-project-context"
    old_skill = _snapshot(skill)
    plugin = config / "plugins/powercontext-opencode.js"
    old_plugin = plugin.read_bytes()
    real_replace = os.replace
    skill_failed = False

    def fail_commit_and_recovery(source, target):
        nonlocal skill_failed
        if Path(target) == skill:
            skill_failed = True
            message = "Skill destination unavailable"
            raise OSError(message)
        if skill_failed and also_fail_plugin_recovery and Path(target) == plugin:
            message = "plugin recovery unavailable"
            raise OSError(message)
        return real_replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_commit_and_recovery)
        with pytest.raises(SetupError, match="recovery failed") as failure:
            opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert not skill.exists()
    backups = list(skill.parent.glob(f".{skill.name}.*.bak"))
    assert len(backups) == 1
    assert _snapshot(backups[0]) == old_skill
    assert str(backups[0]) in str(failure.value)
    transactions = list(config.glob(".powercontext-install-*"))
    if also_fail_plugin_recovery:
        assert len(transactions) == 1
        assert str(transactions[0]) in str(failure.value)
    else:
        assert not transactions
        assert plugin.read_bytes() == old_plugin

    pending = _snapshot(config)
    with pytest.raises(SetupError, match="recovery") as retry:
        opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert str(backups[0]) in str(retry.value)
    for transaction in transactions:
        assert str(transaction) in str(retry.value)
    assert not skill.exists()
    assert _snapshot(config) == pending

    os.replace(backups[0], skill)
    for transaction in transactions:
        old_file = next(path for path in transaction.iterdir() if path.is_file() and path.read_bytes() == old_plugin)
        os.replace(old_file, plugin)
        shutil.rmtree(transaction)
    assert _snapshot(config) == before
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    _assert_success(config, package, True)
    successful = _snapshot(config)
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert _snapshot(config) == successful


@pytest.mark.parametrize("installation", [False], indirect=True)
def test_failed_fresh_install_cleanup_blocks_retry_until_reconciled(installation, monkeypatch) -> None:
    config, package, _upgrade = installation
    before = _snapshot(config)
    plugin = config / "plugins/powercontext-opencode.js"
    real_replace, real_unlink = os.replace, Path.unlink

    def fail_skill_commit(source, target):
        if Path(target) == config / "skills/powercontext-project-context":
            raise OSError("commit")
        return real_replace(source, target)

    def fail_plugin_cleanup(path, *args, **kwargs):
        if path == plugin:
            raise OSError("cleanup")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_skill_commit)
        fault.setattr(Path, "unlink", fail_plugin_cleanup)
        with pytest.raises(SetupError, match="recovery failed"):
            opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert plugin.exists()
    transactions = list(config.glob(".powercontext-install-*"))
    assert len(transactions) == 1
    pending = _snapshot(config)
    with pytest.raises(SetupError, match="recovery") as retry:
        opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert str(transactions[0]) in str(retry.value)
    assert _snapshot(config) == pending
    plugin.unlink()
    shutil.rmtree(transactions[0])
    assert _snapshot(config) == before
    opencode.install_opencode_plugin(source=str(package), ref="unused")
    _assert_success(config, package, False)


@pytest.mark.parametrize("pending", ["multiple", "unowned", "file"])
def test_retry_never_selects_or_overwrites_unresolved_skill_backups(installation, pending: str) -> None:
    config, package, _upgrade = installation
    skill = config / "skills/powercontext-project-context"
    skill.parent.mkdir(parents=True, exist_ok=True)
    backups = [skill.with_name(f".{skill.name}.{index:032x}.bak") for index in range(2 if pending == "multiple" else 1)]
    for backup in backups:
        if pending == "file":
            backup.write_bytes(b"unrecognized recovery data")
        else:
            backup.mkdir()
            (backup / "personal.md").write_text(f"Unique notes from {backup.name}", encoding="utf-8")
    before = _snapshot(config)
    with pytest.raises(SetupError, match="recovery") as failure:
        opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert all(str(backup) in str(failure.value) for backup in backups)
    assert _snapshot(config) == before


@pytest.mark.parametrize("location", ["config", "skills"])
def test_recovery_inspection_error_aborts_before_writes(installation, location: str, monkeypatch) -> None:
    config, package, _upgrade = installation
    unreadable = config if location == "config" else config / "skills"
    unreadable.mkdir(exist_ok=True)
    before = _snapshot(config)
    real_iterdir = Path.iterdir

    def cannot_list(path):
        if path == unreadable:
            message = "recovery directory unreadable"
            raise PermissionError(message)
        return real_iterdir(path)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "iterdir", cannot_list)
        with pytest.raises(SetupError, match="recovery"):
            opencode.install_opencode_plugin(source=str(package), ref="unused")
    assert _snapshot(config) == before
