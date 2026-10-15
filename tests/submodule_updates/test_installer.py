"""Real local Git transactions; no mocked Git or external network."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ha_submodule_update.py"


def git(path, *args):
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "user.name=HA Test",
            "-c",
            "user.email=ha-test@localhost",
            "-C",
            str(path),
            *args,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def installer():
    assert SCRIPT.exists(), "HACS needs a transactional Git-managed installer"
    spec = importlib.util.spec_from_file_location("ha_submodule_update_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    code = source / "custom_components/demo"
    code.mkdir(parents=True)
    for version in ("1.0.0", "2.0.0"):
        (code / "manifest.json").write_text(
            json.dumps({"domain": "demo", "name": "Demo", "version": version})
        )
        (code / "__init__.py").write_text(f'VERSION = "{version}"\n')
        git(source, "add", ".")
        git(source, "commit", "-m", version)
        git(source, "tag", "v" + version)
    root = tmp_path / "ha"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "submodule", "add", str(source), "submodules/demo")
    git(root / "submodules/demo", "checkout", "v1.0.0")
    (root / "custom_components").mkdir()
    (root / "custom_components/demo").symlink_to(
        "../submodules/demo/custom_components/demo", target_is_directory=True
    )
    (root / "integration-sources.json").write_text(
        json.dumps(
            {
                "demo": {
                    "submodule": "submodules/demo",
                    "repository": "owner/demo",
                    "branch": "main",
                }
            }
        )
    )
    (root / ".gitignore").write_text(
        "custom_components/\nbackups/\n.ha-submodule-updates.lock\n"
    )
    (root / "unrelated.txt").write_text("original")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root, source


def install(root, **kw):
    args = dict(
        root=root,
        domain="demo",
        repository="owner/demo",
        ref="v2.0.0",
        ha_version="2026.9.3",
    )
    args.update(kw)
    return installer().install(**args)


def test_update_checks_out_pins_and_deploys_a_plain_directory(repo):
    root, source = repo
    result = install(root)
    target = git(source, "rev-parse", "v2.0.0")
    assert (
        result["managed"]
        and result["commit"] == target
        and result["version"] == "2.0.0"
    )
    assert git(root / "submodules/demo", "rev-parse", "HEAD") == target
    assert target in git(root, "ls-tree", "HEAD", "submodules/demo")
    assert not (root / "custom_components/demo").is_symlink()
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "2.0.0"
    )
    assert list((root / "backups/integration-updates").glob("*/deployed/manifest.json"))


def test_parent_unrelated_staged_work_is_not_committed_or_lost(repo):
    root, _ = repo
    (root / "unrelated.txt").write_text("other-agent")
    git(root, "add", "unrelated.txt")
    install(root)
    assert git(root, "show", "HEAD:unrelated.txt") == "original"
    assert git(root, "diff", "--cached", "--name-only") == "unrelated.txt"
    assert (root / "unrelated.txt").read_text() == "other-agent"


def test_dirty_submodule_is_refused_without_overwrite(repo):
    root, _ = repo
    code = root / "submodules/demo/custom_components/demo/__init__.py"
    code.write_text("my_patch = True\n")
    with pytest.raises(RuntimeError, match="dirty"):
        install(root)
    assert code.read_text() == "my_patch = True\n"


def test_unregistered_repository_cannot_replace_a_fork(repo):
    root, _ = repo
    with pytest.raises(RuntimeError, match="repository"):
        install(root, repository="attacker/demo")
    assert (root / "custom_components/demo").is_symlink()


@pytest.mark.parametrize(
    "ref", ["--upload-pack=evil", "../main", "main:other", "v2.0.0;echo x"]
)
def test_rejects_unsafe_refs(repo, ref):
    with pytest.raises(RuntimeError, match="ref"):
        install(repo[0], ref=ref)


def test_compatibility_check_precedes_checkout_and_deployment(repo):
    root, source = repo
    (source / "hacs.json").write_text(json.dumps({"homeassistant": "2027.1.0"}))
    git(source, "add", ".")
    git(source, "commit", "-m", "requires future HA")
    git(source, "tag", "v3.0.0")
    before = git(root / "submodules/demo", "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="Home Assistant"):
        install(root, ref="v3.0.0")
    assert git(root / "submodules/demo", "rev-parse", "HEAD") == before
    assert (root / "custom_components/demo").is_symlink()


def test_invalid_source_preserves_deployment(repo):
    root, source = repo
    (source / "custom_components/demo/manifest.json").write_text("{broken")
    git(source, "add", ".")
    git(source, "commit", "-m", "broken")
    git(source, "tag", "broken")
    before = git(root / "submodules/demo", "rev-parse", "HEAD")
    with pytest.raises((RuntimeError, ValueError)):
        install(root, ref="broken")
    assert git(root / "submodules/demo", "rev-parse", "HEAD") == before
    assert (root / "custom_components/demo").is_symlink()


def test_commit_failure_rolls_back_checkout_and_deployment(repo):
    root, _ = repo
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = git(root / "submodules/demo", "rev-parse", "HEAD")
    with pytest.raises(RuntimeError):
        install(root)
    assert git(root / "submodules/demo", "rev-parse", "HEAD") == before
    assert (root / "custom_components/demo").is_symlink()
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )


def test_unmanaged_integration_is_left_to_hacs(repo):
    result = install(repo[0], domain="ordinary", repository="owner/ordinary")
    assert result == {"managed": False}


def test_existing_tracked_symlink_is_preserved_when_updating_its_submodule(repo):
    root, source = repo
    git(root, "add", "-f", "custom_components/demo")
    git(root, "commit", "-m", "track integration link")
    install(root)
    assert (root / "custom_components/demo").is_symlink()
    assert git(root, "status", "--porcelain") == ""
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "2.0.0"
    )
    assert git(source, "rev-parse", "v2.0.0") in git(
        root, "ls-tree", "HEAD", "submodules/demo"
    )


def test_pinned_sync_does_not_need_the_remote(repo):
    root, source = repo
    source.rename(source.with_name("offline-source"))
    result = installer().install(
        root=root,
        domain="demo",
        repository="owner/demo",
        ref="HEAD",
        ha_version="2026.9.3",
        pinned=True,
    )
    assert result["version"] == "1.0.0"
    assert not (root / "custom_components/demo").is_symlink()


def test_local_plain_copy_patch_is_not_silently_overwritten(repo):
    import shutil

    root, _ = repo
    deployed = root / "custom_components/demo"
    deployed.unlink()
    shutil.copytree(root / "submodules/demo/custom_components/demo", deployed)
    (deployed / "__init__.py").write_text("local_user_patch = True\n")
    with pytest.raises(RuntimeError, match="deployed"):
        install(root)
    assert (deployed / "__init__.py").read_text() == "local_user_patch = True\n"


def test_protected_branch_cannot_masquerade_as_a_different_release(repo):
    root, _ = repo
    policies = json.loads((root / "integration-sources.json").read_text())
    policies["demo"]["protected_branch"] = "main"
    (root / "integration-sources.json").write_text(json.dumps(policies))
    with pytest.raises(RuntimeError, match="requested release"):
        install(root, ref="v99.0.0")
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )


def test_release_result_does_not_misidentify_tag_as_branch(repo):
    result = install(repo[0])
    assert result["requested_is_branch"] is False


def test_pinned_sync_refuses_unrecorded_checkout(repo):
    root, _ = repo
    git(root / "submodules/demo", "checkout", "v2.0.0")
    with pytest.raises(RuntimeError, match="recorded pin"):
        install(root, pinned=True)


def test_cli_repairs_invalid_home_for_git_hooks(repo):
    import os
    import sys

    root, _ = repo
    hook = root / ".git/hooks/pre-commit"
    hook.write_text('#!/bin/sh\n[ -d "$HOME" ] && [ -w "$HOME" ]\n')
    hook.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--domain",
            "demo",
            "--repository",
            "owner/demo",
            "--ref",
            "v2.0.0",
            "--ha-version",
            "2026.9.3",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(root / "missing-home")},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def run_setup(root, *args, env=None):
    import os
    import sys

    script = SCRIPT.with_name("ha_setup_integrations.py")
    assert script.exists(), "Clone setup must deploy only recorded submodule pins"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--ha-version",
            "2026.9.3",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "GIT_ALLOW_PROTOCOL": "file", **(env or {})},
    )


def test_clone_setup_initializes_exact_pin_and_preserves_tracked_symlink(repo):
    root, source = repo
    git(root, "add", "-f", "custom_components/demo")
    git(root, "commit", "-m", "track link")
    clone = root.parent / "clone"
    git(root.parent, "clone", str(root), str(clone))
    result = run_setup(clone, "--initialize")
    assert result.returncode == 0, result.stderr
    assert (clone / "custom_components/demo").is_symlink()
    assert git(clone / "submodules/demo", "rev-parse", "HEAD") == git(
        source, "rev-parse", "v1.0.0"
    )
    assert git(clone, "status", "--porcelain") == ""


def test_setup_without_initialize_is_offline_and_preserves_plain_copy_patches(repo):
    import shutil

    root, source = repo
    deployed = root / "custom_components/demo"
    deployed.unlink()
    shutil.copytree(root / "submodules/demo/custom_components/demo", deployed)
    (deployed / "__init__.py").write_text("user_patch = True\n")
    source.rename(source.with_name("offline-source"))
    result = run_setup(root, "--target", "demo")
    assert result.returncode != 0
    assert "deployed local changes" in result.stderr
    assert (deployed / "__init__.py").read_text() == "user_patch = True\n"


def test_setup_requires_explicit_initialize_for_missing_submodule(repo):
    root, _ = repo
    clone = root.parent / "clone"
    git(root.parent, "clone", str(root), str(clone))
    result = run_setup(clone)
    assert result.returncode != 0
    assert "--initialize" in result.stderr
    assert not (clone / "custom_components/demo").exists()


def test_sync_wrapper_uses_its_checkout_and_never_production(repo):
    import os
    import sys

    root, _ = repo
    scripts = root / "scripts"
    scripts.mkdir()
    wrapper = SCRIPT.with_name("sync-submodule-to-custom-components.sh")
    import shutil

    for name in (
        "sync-submodule-to-custom-components.sh",
        "ha_setup_integrations.py",
        "ha_submodule_update.py",
    ):
        source = wrapper.with_name(name)
        assert source.exists(), source
        shutil.copy(source, scripts / name)
    result = subprocess.run(
        ["bash", str(scripts / wrapper.name), "demo", "--ha-version", "2026.9.3"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HA_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )


def test_setup_preserves_existing_hook_manager(repo):
    root, _ = repo
    git(root, "config", "core.hooksPath", "/existing/hooks")
    result = run_setup(root, "--install-hooks")
    assert result.returncode != 0
    assert "Existing Git hooks" in result.stderr
    assert git(root, "config", "--get", "core.hooksPath") == "/existing/hooks"


def test_file_checkout_hook_does_not_deploy_or_require_initialized_sources(repo):
    import os

    root, _ = repo
    hook = SCRIPT.parents[1] / ".githooks/post-checkout"
    assert hook.exists()
    result = subprocess.run(
        ["bash", str(hook), "old", "new", "0"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "HOME": str(root)},
    )
    assert result.returncode == 0, result.stderr
    assert (root / "custom_components/demo").is_symlink()
    assert not (root / "backups").exists()


def test_protected_fork_installs_matching_release_without_fetching_upstream_tag(repo):
    root, source = repo
    policies = json.loads((root / "integration-sources.json").read_text())
    policies["demo"]["protected_branch"] = "main"
    (root / "integration-sources.json").write_text(json.dumps(policies))
    git(source, "tag", "-d", "v2.0.0")
    result = install(root, ref="v2.0.0")
    assert result["selected_ref"] == "main"
    assert result["requested_is_branch"] is False
    assert result["version"] == "2.0.0"
    assert result["commit"] == git(source, "rev-parse", "main")


def test_release_without_v_prefix_fetches_exact_tag(repo):
    root, source = repo
    git(source, "tag", "2.0.0")
    result = install(root, ref="2.0.0")
    assert result["selected_ref"] == "2.0.0"
    assert result["commit"] == git(source, "rev-parse", "2.0.0")
    assert result["requested_is_branch"] is False


@pytest.mark.parametrize(
    "event,args", [("post-checkout", ["old", "new", "1"]), ("post-merge", ["0"])]
)
def test_git_hooks_sync_offline_recorded_pin(repo, event, args):
    import os
    import shutil
    import sys

    root, source = repo
    scripts = root / "scripts"
    scripts.mkdir()
    for name in (
        "sync-submodule-to-custom-components.sh",
        "ha_setup_integrations.py",
        "ha_submodule_update.py",
    ):
        shutil.copy(SCRIPT.with_name(name), scripts / name)
    source.rename(source.with_name("offline-source"))
    hook = SCRIPT.parents[1] / ".githooks" / event
    if event == "post-checkout":
        current = git(root, "rev-parse", "HEAD")
        args = [current, current, "1"]
    result = subprocess.run(
        ["bash", str(hook), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            **os.environ,
            "HOME": str(root),
            "HA_VERSION": "2026.9.3",
            "HA_PYTHON": sys.executable,
        },
    )
    assert result.returncode == 0, result.stderr
    assert (root / "custom_components/demo").is_symlink() == (event == "post-checkout")
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )
    assert len(
        list((root / "backups/integration-updates").glob("*/transaction.json"))
    ) == (0 if event == "post-checkout" else 1)


@pytest.mark.parametrize(
    "domain,branch",
    [("webrtc", "master"), ("yandex_dialogs", "master"), ("yandex_smart_home", "dev")],
)
def test_configured_default_branch_is_fetched_as_branch(repo, domain, branch):
    root, source = repo
    git(source, "branch", "-m", "main", branch)
    policy = json.loads((root / "integration-sources.json").read_text())
    project_policy = json.loads(
        (SCRIPT.parents[1] / "integration-sources.json").read_text()
    )
    policy["demo"]["branch"] = project_policy[domain]["branch"]
    (root / "integration-sources.json").write_text(json.dumps(policy))
    result = install(root, ref=branch)
    assert result["commit"] == git(source, "rev-parse", branch)
    assert result["requested_is_branch"] is True


def test_offline_sync_uses_the_previous_parent_pin_for_plain_copy(repo):
    root, _ = repo
    install(root, pinned=True)
    previous = git(root, "rev-parse", "HEAD")
    git(root / "submodules/demo", "checkout", "v2.0.0")
    git(root, "add", "submodules/demo")
    git(root, "commit", "-m", "advance recorded pin")
    result = run_setup(root, "--previous-revision", previous)
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "2.0.0"
    )


def test_offline_sync_still_preserves_patches_against_the_previous_pin(repo):
    root, _ = repo
    install(root, pinned=True)
    previous = git(root, "rev-parse", "HEAD")
    (root / "custom_components/demo/__init__.py").write_text(
        "preserve_my_patch = True\n"
    )
    git(root / "submodules/demo", "checkout", "v2.0.0")
    git(root, "add", "submodules/demo")
    git(root, "commit", "-m", "new pin")
    result = run_setup(root, "--previous-revision", previous)
    assert result.returncode != 0
    assert "deployed local changes" in result.stderr
    assert (
        root / "custom_components/demo/__init__.py"
    ).read_text() == "preserve_my_patch = True\n"


def test_unchanged_pinned_sync_does_not_replace_files_or_add_backup(repo):
    root, _ = repo
    install(root, pinned=True)
    deployed = root / "custom_components/demo"
    before = deployed.stat().st_ino
    backups = list((root / "backups/integration-updates").iterdir())
    result = install(root, pinned=True)
    assert result.get("unchanged") is True
    assert deployed.stat().st_ino == before
    assert list((root / "backups/integration-updates").iterdir()) == backups


def test_checkout_hook_applies_a_changed_pin_without_touching_unrelated_files(repo):
    import shutil, os

    root, _ = repo
    install(root, pinned=True)
    git(root, "switch", "-c", "new-pins")
    git(root / "submodules/demo", "checkout", "v2.0.0")
    git(root, "add", "submodules/demo")
    git(root, "commit", "-m", "new pin")
    target = git(root, "rev-parse", "HEAD")
    git(root, "switch", "main")
    git(root / "submodules/demo", "checkout", "v1.0.0")
    (root / "scripts").mkdir()
    (root / ".githooks").mkdir()
    for name in [
        "ha_setup_integrations.py",
        "ha_submodule_update.py",
        "sync-submodule-to-custom-components.sh",
    ]:
        shutil.copy(SCRIPT.with_name(name), root / "scripts" / name)
    shutil.copy(
        SCRIPT.parents[1] / ".githooks/post-checkout", root / ".githooks/post-checkout"
    )
    git(root, "config", "core.hooksPath", ".githooks")
    proc = subprocess.run(
        ["git", "-C", str(root), "switch", "new-pins"],
        env={**os.environ, "HOME": str(root), "HA_VERSION": "2026.9.3"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert git(root, "rev-parse", "HEAD") == target
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "2.0.0"
    )
    assert (root / "unrelated.txt").read_text() == "original"


def test_git_hook_parent_index_does_not_poison_submodule_commands(repo, monkeypatch):
    root, source = repo
    monkeypatch.setenv("GIT_INDEX_FILE", str(root / ".git/index"))
    result = install(root)
    assert result["version"] == "2.0.0"
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "2.0.0"
    )


def test_repeating_a_parent_transition_is_idempotent(repo):
    root, _ = repo
    install(root, pinned=True)
    previous = git(root, "rev-parse", "HEAD")
    git(root / "submodules/demo", "checkout", "v2.0.0")
    git(root, "add", "submodules/demo")
    git(root, "commit", "-m", "new pin")
    first = run_setup(root, "--previous-revision", previous)
    assert first.returncode == 0, first.stderr
    backups = list((root / "backups/integration-updates").iterdir())
    second = run_setup(root, "--previous-revision", previous)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)[0]["unchanged"] is True
    assert list((root / "backups/integration-updates").iterdir()) == backups


@pytest.mark.parametrize("length", [7, 40])
def test_hacs_commit_version_uses_verified_default_branch_head(repo, length):
    root, source = repo
    expected = git(source, "rev-parse", "main")
    result = install(root, ref=expected[:length])
    assert result["commit"] == expected
    assert result["requested_is_branch"] is True


def test_stale_commit_update_is_rejected_before_deployment(repo):
    root, source = repo
    stale = git(source, "rev-parse", "v1.0.0")
    with pytest.raises(RuntimeError, match="commit"):
        install(root, ref=stale[:7])
    assert (
        json.loads((root / "custom_components/demo/manifest.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )
