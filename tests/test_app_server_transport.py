import asyncio
import json
import sys
from pathlib import Path

from codex_supervisor.app_server import CodexAppServer


def test_stdio_initialize_handshake_and_rate_limit_read(tmp_path):
    fake = tmp_path / "fake_app_server.py"
    fake.write_text(
        '''
import json, sys
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "account/rateLimits/read":
        if not initialized:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32000,"message":"not initialized"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{
                "ordinaryUsageAllowed": True,
                "rateLimits": {"limitId":"codex","primary":{"usedPercent":12,"windowDurationMins":300,"resetsAt":2000}}
            }}), flush=True)
'''
    )

    async def scenario():
        client = CodexAppServer((sys.executable, str(fake)))
        await client.start()
        try:
            payload = await client.read_rate_limits()
            assert payload["ordinaryUsageAllowed"] is True
            assert payload["rateLimits"]["primary"]["usedPercent"] == 12
        finally:
            await client.close()

    asyncio.run(scenario())


def test_thread_list_uses_state_db_fast_path(tmp_path):
    fake = tmp_path / "fake_app_server_threads.py"
    fake.write_text(
        '''
import json, sys
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "thread/list":
        params = msg.get("params") or {}
        if not initialized:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32000,"message":"not initialized"}}), flush=True)
        elif params.get("useStateDbOnly") is not True:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32001,"message":"slow path used"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{
                "data":[{
                    "id":"thread-1",
                    "preview":"Inspect Brain understanding",
                    "cwd":"C:/repo",
                    "updatedAt":1791320000
                }]
            }}), flush=True)
'''
    )

    async def scenario():
        client = CodexAppServer((sys.executable, str(fake)))
        await client.start()
        try:
            items = await client.list_threads(limit=100)
            assert items[0]["id"] == "thread-1"
            assert items[0]["preview"] == "Inspect Brain understanding"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_thread_list_full_scan_can_be_requested(tmp_path):
    fake = tmp_path / "fake_app_server_threads_scan.py"
    fake.write_text(
        '''
import json, sys
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "thread/list":
        params = msg.get("params") or {}
        if params.get("useStateDbOnly") is not False:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32001,"message":"fast path used"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"data":[]}}), flush=True)
'''
    )

    async def scenario():
        client = CodexAppServer((sys.executable, str(fake)))
        await client.start()
        try:
            items = await client.list_threads(limit=100, state_db_only=False)
            assert items == []
        finally:
            await client.close()

    asyncio.run(scenario())




def test_thread_list_supports_targeted_cwd_and_title_filter(tmp_path):
    fake = tmp_path / "fake_app_server_targeted.py"
    fake.write_text(
        """
import json, sys
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "thread/list":
        params = msg.get("params") or {}
        ok = (
            initialized
            and params.get("useStateDbOnly") is True
            and params.get("cwd") == "C:/repo/DEMO_V1"
            and params.get("searchTerm") == "Inspect Brain"
            and int(params.get("limit") or 0) <= 20
        )
        if not ok:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32001,"message":"query was not targeted"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"data":[{
                "id":"thread-target",
                "name":"Inspect Brain understanding",
                "cwd":"C:/repo/DEMO_V1",
                "updatedAt":1791320000
            }]}}), flush=True)
"""
    )

    async def scenario():
        client = CodexAppServer((sys.executable, str(fake)))
        await client.start()
        try:
            items = await client.list_threads(
                limit=20,
                search_term="Inspect Brain",
                cwd="C:/repo/DEMO_V1",
                state_db_only=True,
            )
            assert [item["id"] for item in items] == ["thread-target"]
        finally:
            await client.close()

    asyncio.run(scenario())
