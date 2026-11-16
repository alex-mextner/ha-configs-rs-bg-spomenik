#!/usr/bin/env python3
"""Report browser memory use for the existing host resource guard."""

import os
from pathlib import Path

LIMIT_RSS_KB = int(os.getenv("AGENT_BROWSER_GUARD_RSS_KB", str(4 * 1024 * 1024)))
LIMIT_SWAP_KB = int(os.getenv("AGENT_BROWSER_GUARD_SWAP_KB", str(1536 * 1024)))
PREFIX = "/home/ultra/.agent-browser/browsers/"


def usage() -> tuple[int, int, int]:
    """Return the browser process count, resident memory, and swap in KiB."""
    rss = swap = count = 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
            if PREFIX not in cmd:
                continue
            status = (proc / "status").read_text()
        except (OSError, PermissionError):
            continue
        vals = {}
        for line in status.splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0])
        rss += vals.get("VmRSS", 0)
        swap += vals.get("VmSwap", 0)
        count += 1
    return count, rss, swap


if __name__ == "__main__":
    count, rss, swap = usage()
    print(f"agent-browser chrome: processes={count} rss_kb={rss} swap_kb={swap}")
    raise SystemExit(42 if rss > LIMIT_RSS_KB or swap > LIMIT_SWAP_KB else 0)
