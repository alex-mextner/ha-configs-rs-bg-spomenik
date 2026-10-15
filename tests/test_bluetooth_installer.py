# Copyright 2026 Project contributors
# ruff: noqa: INP001, PT009, S603
"""
End-to-end regression tests for the Bluetooth installer and host preparation.

The installer normally requires root and writes host files.  Each test transforms
only those absolute target paths into a temporary filesystem, then executes the
real shell flow under fakeroot.  Docker, systemd, and adapter commands are the
only faked process boundaries.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKEROOT = shutil.which("fakeroot")


def fakeroot_path() -> str:
    """Return the resolved wrapper before a test replaces PATH with fakes."""
    if FAKEROOT is None:
        message = "fakeroot is required for installer tests"
        raise RuntimeError(message)
    return FAKEROOT


class BluetoothInstallerTests(unittest.TestCase):
    """Exercise installer publication order without writing the real host paths."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.sandbox = Path(self.temporary_directory.name)
        self.application_root = self.sandbox / "application"
        self.fake_bin = self.sandbox / "fake-bin"
        self.settings_path = self.sandbox / "system/etc/default/ha-compose"
        self.unit_path = self.sandbox / "system/etc/systemd/system/ha-compose.service"
        self.helper_path = self.sandbox / "system/usr/local/libexec/ha-bluetooth-host"
        self.backup_path = self.sandbox / "system/var/backups/ha-bluetooth"
        self.call_log = self.sandbox / "calls.log"
        self.settings_path.parent.mkdir(parents=True)
        self.unit_path.parent.mkdir(parents=True)
        self.helper_path.parent.mkdir(parents=True)
        self._copy_application()
        self._write_command_fakes()

    def _copy_application(self) -> None:
        for relative_path in (
            "scripts/install_ha_bluetooth.sh",
            "scripts/ha_bluetooth_host.sh",
            "scripts/ha_compose.sh",
            "scripts/host/ha-compose.service",
        ):
            source = ROOT / relative_path
            target = self.application_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        self._redirect_system_paths(
            self.application_root / "scripts/install_ha_bluetooth.sh"
        )
        self._prepend_fake_path(self.application_root / "scripts/ha_bluetooth_host.sh")

    def _redirect_system_paths(self, installer: Path) -> None:
        content = installer.read_text()
        replacements = {
            "/etc/default/ha-compose": str(self.settings_path),
            "/etc/systemd/system/ha-compose.service": str(self.unit_path),
            "/usr/local/libexec/ha-bluetooth-host": str(self.helper_path),
            "/var/backups/ha-bluetooth": str(self.backup_path),
        }
        for source, target in replacements.items():
            content = content.replace(source, target)
        installer.write_text(content)
        self._prepend_fake_path(installer)

    def _prepend_fake_path(self, script: Path) -> None:
        content = script.read_text()
        original = (
            "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        replacement = f"export PATH={self.fake_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertIn(original, content)
        script.write_text(content.replace(original, replacement))

    def _write_command_fakes(self) -> None:
        self.fake_bin.mkdir()
        commands = {
            "docker": """#!/bin/sh
if [ \"$1\" = inspect ]; then
  printf '%s\\n' '<no value>'
  exit 0
fi
case \" $* \" in
  *' config '*)
    if [ \"${FAIL_COMPOSE:-}\" = 1 ]; then
      echo 'compose validation failed' >&2
      exit 75
    fi
    ;;
esac
exit 0
""",
            "systemctl": """#!/bin/sh
printf '%s\\n' \"$*\" >> \"$CALL_LOG\"
if [ \"$1\" = enable ] && [ \"$2\" = ha-compose.service ] && [ \"${FAIL_ENABLE:-}\" = 1 ]; then
  exit 77
fi
if [ \"$1\" = enable ] && [ \"$2\" = --now ] && [ \"$3\" = bluetooth.service ] && [ \"${FAIL_BLUETOOTH:-}\" = 1 ]; then
  exit 78
fi
exit 0
""",
            "install": """#!/usr/bin/env bash
printf 'install %s\\n' \"$*\" >> \"$CALL_LOG\"
arguments=()
while (($#)); do
  case \"$1\" in
    -o|-g) shift 2 ;;
    *) arguments+=(\"$1\"); shift ;;
  esac
done
exec /usr/bin/install \"${arguments[@]}\"
""",
            "stat": """#!/bin/sh
if [ \"$1\" = -c ] && [ \"$2\" = %U ]; then
  printf '%s\\n' ultra
  exit 0
fi
exec /usr/bin/stat \"$@\"
""",
            "bluetoothd": "#!/bin/sh\nexit 0\n",
            "dbus-send": "#!/bin/sh\nexit 0\n",
            "rfkill": "#!/bin/sh\nexit 0\n",
            "bluetoothctl": """#!/bin/sh
case \"$*\" in
  *' list'*)
    if [ \"${CONTROLLER_PRESENT:-}\" = 1 ]; then
      echo 'Controller AA:BB:CC:DD:EE:FF Test adapter'
    fi
    exit 0
    ;;
  *' power on'*)
    [ \"${POWER_ON_FAIL:-}\" != 1 ]
    exit
    ;;
esac
exit 0
""",
        }
        for name, content in commands.items():
            command = self.fake_bin / name
            command.write_text(content)
            command.chmod(0o755)

    def _seed_published_files(self) -> tuple[bytes, bytes, bytes]:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.helper_path.parent.mkdir(parents=True, exist_ok=True)
        settings = b"# Keep this exact file\nHA_COMPOSE_PROJECT_NAME=living-room\n"
        unit = b"[Service]\nExecStart=/existing/unit\n"
        helper = b"#!/bin/sh\necho existing-helper\n"
        self.settings_path.write_bytes(settings)
        self.settings_path.chmod(0o640)
        self.unit_path.write_bytes(unit)
        self.unit_path.chmod(0o600)
        self.helper_path.write_bytes(helper)
        self.helper_path.chmod(0o700)
        return settings, unit, helper

    def _run_installer(
        self,
        *overlays: Path,
        current_directory: Path | None = None,
        **environment: str,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            PATH=f"{self.fake_bin}:{os.environ['PATH']}",
            CALL_LOG=str(self.call_log),
        )
        env.update(environment)
        return subprocess.run(
            [
                fakeroot_path(),
                "bash",
                str(self.application_root / "scripts/install_ha_bluetooth.sh"),
                *(str(overlay) for overlay in overlays),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            cwd=current_directory,
        )

    def test_quoted_extra_command_is_rejected_before_shell_sourcing(self) -> None:
        self._seed_published_files()
        marker = self.sandbox / "unexpected-command"
        self.settings_path.write_text(
            f'HA_COMPOSE_PROJECT_NAME="room" touch "{marker}"\n'
        )
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists(), "Settings must not execute a second word")

    def test_fresh_host_creates_missing_helper_directory(self) -> None:
        self.helper_path.parent.rmdir()
        result = self._run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.helper_path.is_file())
        self.assertEqual(stat.S_IMODE(self.helper_path.stat().st_mode), 0o755)

    def test_failed_compose_validation_preserves_published_machine_settings(
        self,
    ) -> None:
        settings, unit, helper = self._seed_published_files()
        overlay = self.sandbox / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(overlay, FAIL_COMPOSE="1")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings_path.read_bytes(), settings)
        self.assertEqual(self.unit_path.read_bytes(), unit)
        self.assertEqual(self.helper_path.read_bytes(), helper)
        self.assertEqual(stat.S_IMODE(self.settings_path.stat().st_mode), 0o640)

    def test_prerequisite_failure_occurs_before_any_publication(self) -> None:
        settings, unit, helper = self._seed_published_files()
        overlay = self.sandbox / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(overlay, FAIL_BLUETOOTH="1")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings_path.read_bytes(), settings)
        self.assertEqual(self.unit_path.read_bytes(), unit)
        self.assertEqual(self.helper_path.read_bytes(), helper)

    def test_rerun_without_overlays_preserves_existing_settings_byte_for_byte(
        self,
    ) -> None:
        settings, _, _ = self._seed_published_files()

        result = self._run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings_path.read_bytes(), settings)
        self.assertEqual(stat.S_IMODE(self.settings_path.stat().st_mode), 0o640)

    def test_missing_docker_label_creates_empty_bootable_settings(self) -> None:
        result = self._run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        settings = self.settings_path.read_text()
        self.assertNotIn("HA_COMPOSE_OVERLAYS", settings)

    def test_relative_overlay_is_published_as_an_absolute_path(self) -> None:
        overlay = self.application_root / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(
            Path("machine.yaml"), current_directory=self.application_root
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"HA_COMPOSE_OVERLAYS={overlay}", self.settings_path.read_text())

    def test_unsafe_existing_settings_are_rejected_before_shell_sourcing(self) -> None:
        marker = self.sandbox / "settings-were-sourced"
        settings = f"HA_COMPOSE_PROJECT_NAME=$(touch {marker})\n".encode()
        self.settings_path.write_bytes(settings)

        result = self._run_installer()

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("Unsafe Compose settings", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.settings_path.read_bytes(), settings)

    def test_activation_failure_restores_all_previously_published_files(self) -> None:
        settings, unit, helper = self._seed_published_files()
        overlay = self.sandbox / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(overlay, FAIL_ENABLE="1")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings_path.read_bytes(), settings)
        self.assertEqual(self.unit_path.read_bytes(), unit)
        self.assertEqual(self.helper_path.read_bytes(), helper)
        self.assertEqual(stat.S_IMODE(self.unit_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.helper_path.stat().st_mode), 0o700)
        self.assertIn("enable ha-compose.service", self.call_log.read_text())

    def test_activation_failure_on_fresh_install_leaves_no_published_files(
        self,
    ) -> None:
        overlay = self.sandbox / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(overlay, FAIL_ENABLE="1")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.unit_path.exists())
        self.assertFalse(self.helper_path.exists())

    def test_install_copies_immutable_helper_and_unit_uses_it(self) -> None:
        overlay = self.sandbox / "machine.yaml"
        overlay.write_text("services: {}\n")

        result = self._run_installer(overlay)

        self.assertEqual(result.returncode, 0, result.stderr)
        rendered_unit = self.unit_path.read_text()
        self.assertIn(f"ExecStartPre=+{self.helper_path}", rendered_unit)
        self.assertNotIn("ha_bluetooth_host.sh", rendered_unit)
        self.assertEqual(
            self.helper_path.read_text(),
            (self.application_root / "scripts/ha_bluetooth_host.sh").read_text(),
        )
        self.assertEqual(stat.S_IMODE(self.helper_path.stat().st_mode), 0o755)
        self.assertIn("install -o root -g root -m 755", self.call_log.read_text())


class BluetoothHostPreparationTests(unittest.TestCase):
    """Verify adapter failures stay optional after D-Bus and BlueZ are prepared."""

    def test_powered_off_adapter_does_not_fail_host_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sandbox = Path(temporary_directory)
            fake_bin = sandbox / "fake-bin"
            fake_bin.mkdir()
            host_script = sandbox / "ha_bluetooth_host.sh"
            content = (ROOT / "scripts/ha_bluetooth_host.sh").read_text()
            content = content.replace(
                "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f"export PATH={fake_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            )
            host_script.write_text(content)
            host_script.chmod(0o755)
            for name, script in {
                "bluetoothd": "#!/bin/sh\nexit 0\n",
                "dbus-send": "#!/bin/sh\nexit 0\n",
                "rfkill": "#!/bin/sh\nexit 0\n",
                "systemctl": "#!/bin/sh\nexit 0\n",
                "bluetoothctl": """#!/bin/sh
case \"$*\" in
  *' list'*) echo 'Controller AA:BB:CC:DD:EE:FF Test adapter'; exit 0 ;;
  *' power on'*) exit 6 ;;
esac
exit 0
""",
            }.items():
                command = fake_bin / name
                command.write_text(script)
                command.chmod(0o755)

            result = subprocess.run(
                [fakeroot_path(), "bash", str(host_script)],
                text=True,
                capture_output=True,
                check=False,
                env=dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}"),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not be powered on", result.stderr)


if __name__ == "__main__":
    unittest.main()
