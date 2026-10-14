#!/usr/bin/env python3
"""Opt-in shared browser lifecycle; legacy deployments remain unchanged."""
import asyncio
import os


def main():
    import bridge
    mode = os.environ.get('CHATGPT_WEB_BROWSER_MODE', 'legacy')
    if mode == 'legacy':
        asyncio.run(bridge.main())
        return
    if mode not in ('manual', 'managed'):
        raise SystemExit('CHATGPT_WEB_BROWSER_MODE must be legacy, manual, or managed.')
    try:
        from agenttools_chatgpt.ha_bridge import run
    except ImportError as exc:
        raise SystemExit('Shared agent-tools library missing: mount its lib directory at '
                         '/opt/agent-tools/lib (read-only), or install the reviewed wheel. '
                         'No silent fallback to a different browser mode.') from exc
    asyncio.run(run(bridge))


if __name__ == '__main__':
    main()
