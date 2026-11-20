"""A stdlib MCP peer for testing legacy manual connection migration."""
import json
import os
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(json.dumps({
    'token_present': os.environ.get('FIXTURE_TOKEN') == 'synthetic-explicit-token',
    'host_secret_present': 'UNRELATED_HOST_CREDENTIAL' in os.environ,
    'cwd': os.getcwd(),
    'pid': os.getpid(),
}), encoding='utf-8')
if os.environ.get('FIXTURE_TOKEN') != 'synthetic-explicit-token':
    sys.exit(3)

for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'],
            'serverInfo': {'name': 'manual-environment-fixture', 'version': '1.0.0'}, 'capabilities': {'tools': {}}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Synthetic echo',
            'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': message['params']['arguments']['text']}], 'isError': False}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
            'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
