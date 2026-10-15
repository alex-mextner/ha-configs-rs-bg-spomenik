"""Resolve the GUI profile without recreating an absent external volume."""
import os
from pathlib import Path


def filesystem_type(path: Path) -> str:
    resolved = path.resolve()
    matches = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields, filesystem = line.split(' - ', 1)
        mount = Path(fields.split()[4].replace('\\040', ' '))
        if resolved.is_relative_to(mount):
            matches.append((len(str(mount)), filesystem.split()[0]))
    return max(matches, default=(0, 'unknown'))[1]


def resolve_profile(persistent: Path, runtime: Path, ephemeral: bool = False) -> Path:
    if ephemeral:
        if not runtime.is_dir() or filesystem_type(runtime) != 'tmpfs':
            raise RuntimeError('Temporary desktop profile requires an existing tmpfs runtime directory')
        profile = runtime / 'chatgpt-native-ha/profile'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        return profile
    if not persistent.is_dir() or not persistent.parent.parent.is_mount():
        raise RuntimeError('Persistent profile volume is absent; mount it or explicitly enable a temporary RAM profile')
    return persistent


if __name__ == '__main__':
    print(resolve_profile(
        Path(os.environ.get('CHATGPT_NATIVE_PROFILE', '/run/media/ultra/WD500/ultra-work/chatgpt-native')),
        Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')),
        os.environ.get('CHATGPT_NATIVE_EPHEMERAL') == '1',
    ))
