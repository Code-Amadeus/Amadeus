"""Opt-in unmodified DSH/MCP stdio reproduction with a local model fixture.

No external API/key. Emits two deterministic MCP resource calls and captures
only non-JSON stdout, forwarding the stream unchanged to the real ACP SDK.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / 'build/dsh-stdio-diagnosis')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'build/deepseek-acp-live/runtime')
    parser.add_argument('--expect-clean', action='store_true', help='Fail if the stdout protocol contains any non-JSON line')
    args = parser.parse_args()
    state, runtime = args.state.resolve(), args.runtime.resolve()
    if ROOT / 'build' not in state.parents:
        parser.error('State must be an isolated directory inside checkout/build')
    state.mkdir(parents=True, exist_ok=True)
    (state / 'workspace').mkdir(exist_ok=True)
    (state / 'archive').mkdir(exist_ok=True)
    trace = state / 'non-json.txt'
    trace.write_text('', encoding='utf-8')
    requests, results = [], []
    tools_to_call = ['list_mcp_resources', 'list_mcp_resource_templates']

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('count_tokens'):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            names = [t['name'] for t in body.get('tools', [])]
            index = len(requests)
            if tools_to_call[0] in names:
                requests.append(body)
            else:
                index = 2  # Background title requests finish without tools.
            content = ({'type': 'tool_use', 'id': f'fixture-{index}', 'name': tools_to_call[index],
                'input': {'server': 'archive'}} if index < 2
                else {'type': 'text', 'text': 'Synthetic resource checks complete.'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            def event(kind, **fields):
                data = json.dumps({'type': kind, **fields})
                self.wfile.write(f'event: {kind}\ndata: {data}\n\n'.encode())
            event('message_start', message={'id': f'msg-{index}', 'type': 'message', 'role': 'assistant',
                'model': body['model'], 'content': [], 'usage': {'input_tokens': 100, 'output_tokens': 0}})
            event('content_block_start', index=0, content_block=content)
            event('content_block_stop', index=0)
            event('message_delta', delta={'stop_reason': 'tool_use' if index < 2 else 'end_turn'}, usage={'output_tokens': 10})
            event('message_stop')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    from agent_host.acp_configuration import AcpAgentSpec
    from agent_host.adapters.acp import AcpProviderAdapter
    from agent_host.mcp_connections import McpConnectionSpec
    from agent_host.provider_types import ProviderRunRequest, ProviderPermissionResponse
    patch = state / 'local-model.json'
    patch.write_text(json.dumps([
        {'id': 'llm-deepseek', 'config': {'baseURL': f'http://127.0.0.1:{server.server_port}', 'maxTokens': 1024}},
        {'id': 'tool-plugin-manager', 'disabled': True},
    ]), encoding='utf-8')
    os.environ['AMADEUS_DIAG_KEY'] = 'synthetic-not-a-credential'
    os.environ['AMADEUS_DIAG_HOME'] = str(state / 'home')
    node = shutil.which('node')
    spec = AcpAgentSpec('dsh_diag', 'DSH local diagnostic', node, args=(
        str(ROOT / 'tools/probes/acp_stdout_tap.mjs'), str(trace), node,
        str(runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js'), '--profile', 'acp', '--patch', str(patch)),
        enabled=True, environment={'DEEPSEEK_API_KEY': 'AMADEUS_DIAG_KEY', 'DSH_HOME': 'AMADEUS_DIAG_HOME'},
        config_options={'reasoning_effort': 'low'})
    connection = McpConnectionSpec('archive', 'Synthetic filesystem', 'stdio', True, ('dsh_diag',), node,
        (str(runtime / 'node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'), str(state / 'archive')))
    adapter = AcpProviderAdapter(spec, mcp_connections=(connection,), setup_timeout=30, run_timeout=45)
    async def emit(event):
        if event.type == 'permission.requested':
            await adapter.resolve_permission('stdio-diagnostic', ProviderPermissionResponse(event.payload['permissionRequest']['request_id'], False))
        if event.type == 'tool.result':
            results.append({'name': event.payload.get('name'), 'success': event.payload.get('success')})
    try:
        result = await adapter.run(ProviderRunRequest('dsh_diag', 'Inspect the synthetic archive resource catalog only.', cwd=str(state / 'workspace')), 'stdio-diagnostic', emit)
        lines = trace.read_text(encoding='utf-8').splitlines()
        expected_tools = [row['name'] for row in results] == tools_to_call and all(row['success'] for row in results)
        report = {'status': result.status, 'error': result.error, 'model_requests': len(requests),
            'expected_tools_observed': expected_tools, 'clean_wire': not lines,
            'tool_results': results, 'non_json_lines': [line.replace(str(ROOT), '<checkout>') for line in lines]}
        (state / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        return 0 if result.status == 'done' and expected_tools and (not args.expect_clean or not lines) else 1
    finally:
        await adapter.close()
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
