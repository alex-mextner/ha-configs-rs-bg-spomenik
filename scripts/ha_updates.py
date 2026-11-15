#!/usr/bin/env python3
"""Inspect or install HA updates using public HA APIs; never edit .storage."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import aiohttp


def load_token(env_file: Path) -> str:
    if token := os.environ.get('HA_TOKEN'):
        return token
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, separator, value = line.removeprefix('export ').partition('=')
            if separator and key.strip() == 'HA_TOKEN':
                return value.strip().strip('\"\'')
    raise RuntimeError('HA_TOKEN is required in the environment or the selected env file')


async def websocket_call(client, url, token, command):
    async with client.ws_connect(url.rstrip('/') + '/api/websocket') as ws:
        await ws.receive_json()
        await ws.send_json({'type': 'auth', 'access_token': token})
        auth = await ws.receive_json()
        if auth.get('type') != 'auth_ok':
            raise RuntimeError('Home Assistant authentication failed')
        await ws.send_json({'id': 1, **command})
        while True:
            reply = await ws.receive_json()
            if reply.get('id') == 1:
                if not reply.get('success'):
                    raise RuntimeError(str(reply.get('error')))
                return reply.get('result')


async def run(args):
    token = load_token(args.env_file)
    url = args.url.rstrip('/')
    headers = {'Authorization': 'Bearer ' + token}
    timeout = aiohttp.ClientTimeout(total=900, connect=15)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        if args.action == 'repositories':
            repos = await websocket_call(client, url, token, {'type': 'hacs/repositories/list', 'categories': ['integration']})
            return [{key: item.get(key) for key in ['domain', 'full_name', 'installed_version', 'available_version', 'homeassistant', 'id']} for item in repos if item.get('installed')]
        if args.action == 'states':
            async with client.get(url + '/api/states', headers=headers) as response:
                response.raise_for_status()
                states = await response.json()
            return [{'entity_id': item['entity_id'], 'state': item['state'], 'installed_version': item['attributes'].get('installed_version'), 'latest_version': item['attributes'].get('latest_version')} for item in states if item['entity_id'].startswith('update.')]
        if args.action == 'registry':
            entries = await websocket_call(client, url, token, {'type': 'config/entity_registry/list'})
            return [{key: item.get(key) for key in ['entity_id', 'unique_id', 'platform']} for item in entries if item['entity_id'].startswith('update.') and item.get('platform') == 'hacs']
        if args.action == 'install':
            if not args.entity or not args.entity.startswith('update.'):
                raise RuntimeError('An explicit update.* entity is required')
            data = {'entity_id': args.entity}
            if args.version:
                data['version'] = args.version
            async with client.post(url + '/api/services/update/install', json=data, headers=headers) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f'HA update.install failed ({response.status}): {body[:1200]}')
                return {'accepted': True, 'entity_id': args.entity}
        if args.action == 'probe':
            async with client.get(url + '/api/chatgpt_native/vnc.html') as response:
                anonymous_status = response.status
            session = await websocket_call(client, url, token, {'type': 'ha-ingress/session'})
            async with client.post(url + '/api/chatgpt_native/session', json=session, headers=headers) as response:
                response.raise_for_status()
            async with client.get(url + '/api/chatgpt_native/vnc.html') as response:
                html = await response.text()
                if response.status != 200 or 'noVNC' not in html:
                    raise RuntimeError(f'Authenticated noVNC HTML failed ({response.status})')
            async with client.ws_connect(url + '/api/chatgpt_native/websockify', protocols=['binary']) as ws:
                message = await ws.receive(timeout=10)
                if message.type != aiohttp.WSMsgType.BINARY or not message.data.startswith(b'RFB '):
                    raise RuntimeError('Authenticated VNC WebSocket did not deliver RFB')
                banner = message.data.decode('ascii').strip()
            if anonymous_status != 401:
                raise RuntimeError(f'Anonymous access was not rejected: {anonymous_status}')
            return {'anonymous': anonymous_status, 'admin_html': 200, 'admin_websocket': banner}
    raise RuntimeError('Unknown action')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['repositories', 'states', 'registry', 'install', 'probe'])
    parser.add_argument('--url', default=os.environ.get('HA_URL', 'http://127.0.0.1:8123'))
    parser.add_argument('--env-file', type=Path, default=Path.home() / '.env')
    parser.add_argument('--entity')
    parser.add_argument('--version')
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
