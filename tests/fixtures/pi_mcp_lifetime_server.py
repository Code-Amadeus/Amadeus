"""合成 MCP 生命周期：独占锁只在 EOF/finally 正常释放，原文件保留。"""
import json
import os
from pathlib import Path
import sys


root = Path(sys.argv[1])
lock = (root / 'exclusive.lock').open('a+b')
if lock.tell() == 0:
    lock.write(b'0')
    lock.flush()
lock.seek(0)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
(root / 'started.json').write_text(json.dumps({'pid': os.getpid()}), encoding='utf-8')
eof = False
try:
    for raw in sys.stdin:
        message = json.loads(raw)
        if 'id' not in message:
            continue
        method = message.get('method')
        if method == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'lifetime-fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'inspect_fixture', 'description': 'Synthetic no-op.',
                'inputSchema': {'type': 'object', 'properties': {}}}]}
        elif method == 'tools/call':
            (root / 'called').write_text('called', encoding='utf-8')
            result = {'content': [{'type': 'text', 'text': 'Synthetic fixture only.'}]}
        elif method in ('resources/list', 'resources/templates/list', 'prompts/list'):
            key = {'resources/list': 'resources', 'resources/templates/list': 'resourceTemplates', 'prompts/list': 'prompts'}[method]
            result = {key: []}
        elif method == 'ping':
            result = {}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                'error': {'code': -32601, 'message': 'Unsupported synthetic method'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
    eof = True
    (root / 'stdin-eof').write_text('eof', encoding='utf-8')
finally:
    lock.close()
    (root / 'finally.json').write_text(json.dumps({'stdin_eof': eof, 'lock_released': True}), encoding='utf-8')
