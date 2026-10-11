"""Opt-in real-model ACP plugin probe over disposable, installer-owned data."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import time

from dotenv import dotenv_values
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'native', 'update', 'persistence', 'remove', 'web-off'])
    parser.add_argument('--state', type=Path, default=ROOT / 'build/deepseek-acp-live')
    parser.add_argument('--env-file', type=Path, help='Optional local credential source; values are never printed')
    args = parser.parse_args()
    from agent_host.acp_configuration import AcpAgentSpec
    from agent_host.adapters.acp import AcpProviderAdapter
    from agent_host.extension_mcp import project_extension_mcp_connections
    from agent_host.mcp_connections import McpConnectionSpec
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_types import ProviderRunRequest, ProviderPermissionResponse, ProviderSessionHandle
    from agent_host.skills import SkillCatalog, configure_skills
    from server.extensions.package import validate_installed
    base = args.state.resolve()
    if ROOT / 'build' not in base.parents:
        parser.error('--state must be a dedicated directory under this checkout/build')
    if not (base / 'prepare-snapshot.json').is_file():
        parser.error('Prepare installer snapshots before running this opt-in paid probe')
    work = base / 'workspace'
    work.mkdir(exist_ok=True)
    archive = base / 'archive'
    archive.mkdir(exist_ok=True)
    (archive/'catalog.json').write_text(json.dumps({'marker':'catalog-7f3c91','items':['lamp','notebook']}),encoding='utf-8')
    (work/'facts.json').write_text(json.dumps({'changes':['Added CSV import'], 'verification':['Unit checks passed'], 'remaining':['Human review']}),encoding='utf-8')
    (work/'orders.csv').write_text('id,amount\nA,10\nB,-3\nA,5\n,7\nC,\n',encoding='utf-8')
    key = os.environ.get('DEEPSEEK_API_KEY') or (dotenv_values(args.env_file).get('DEEPSEEK_API_KEY') if args.env_file else None)
    if not key:
        raise RuntimeError('DeepSeek key not configured')
    os.environ['AMADEUS_DSH_LIVE_KEY'] = key
    os.environ['AMADEUS_DSH_LIVE_HOME'] = str(base/'home')
    snapshot_phase = {'native':'prepare', 'persistence':'update', 'web-off':'remove'}.get(args.phase,args.phase)
    records = json.loads((base/(snapshot_phase+'-snapshot.json')).read_text(encoding='utf-8'))
    roots = [str(Path(r['package_path'])/'skills') for r in records if r['manifest']['contributes'].get('skill')] if args.phase == 'native' else []
    patch = base/'live.patch.yml'
    patch.write_text('- id: skill-filesystem\n  config:\n    includeDefaultRoots: false\n    customSkillDirs: '+json.dumps(roots)+'\n'
                     '- id: tool-plugin-manager\n  disabled: true\n'
                     '- id: llm-deepseek\n  config:\n    maxTokens: 4096\n'
                     + ('- id: tool-web\n  disabled: true\n' if args.phase == 'web-off' else ''),encoding='utf-8')
    for record in records:
        validate_installed(record['manifest'],Path(record['package_path']))
    configure_skills(SkillCatalog(records))
    entry = base/'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'
    spec = AcpAgentSpec('deepseek_live','DeepSeek Harness Live',shutil.which('node'),
        args=(str(entry),'--profile','acp','--patch',str(patch)), enabled=True,resume=True,
        environment={'DEEPSEEK_API_KEY':'AMADEUS_DSH_LIVE_KEY','DSH_HOME':'AMADEUS_DSH_LIVE_HOME'},
        config_options={'reasoning_effort':'low'})
    connections = [c for record in records for c in project_extension_mcp_connections(record)]
    connections.append(McpConnectionSpec('archive','Fixture file service','stdio',True,('deepseek_live',),
        shutil.which('node'),(str(base/'runtime/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'),str(archive))))
    results = []
    sessions = {}
    children = {}
    allowed_reads = [work] + [Path(record['package_path'])/'skills' for record in records]
    def within(path, root):
        return path == root or root in path.parents

    async def run(name, task, *, session=None):
        adapter = AcpProviderAdapter(spec,mcp_connections=tuple(connections),setup_timeout=45,run_timeout=120,permission_timeout=5)
        events = []
        started = time.monotonic()
        async def emit(event):
            payload = event.payload
            events.append({'type':event.type,'payload':payload})
            for child in psutil.Process().children(recursive=True):
                try:
                    children[child.pid] = child.create_time()
                except psutil.NoSuchProcess:
                    pass
            if event.type == 'permission.requested':
                permission = payload['permissionRequest']
                scope = permission.get('scope',[])
                paths = [Path(p).resolve() for p in scope]
                roots = [work] if permission['capability'] == 'filesystem.write' else allowed_reads
                allow = bool(paths) and permission['capability'] != 'shell.execute' and all(any(within(p,r) for r in roots) for p in paths)
                await adapter.resolve_permission(name,ProviderPermissionResponse(permission['request_id'],allow))
                print(json.dumps({'case':name,'permission':permission['capability'],'allowed':allow}),flush=True)
            elif event.type in {'tool.call','tool.result'}:
                print(json.dumps({'case':name,'event':event.type,'name':payload.get('name'),'success':payload.get('success')}),flush=True)
        request = ProviderRunRequest('deepseek_live',task + '\nUse the available file or service tools. Do not run shell commands, delegate, or modify application settings.',cwd=str(work),session=session,
            requirements=ProviderRequirements(workspace_access='write',workspace_ownership='caller'))
        try:
            result = await adapter.run(request,name,emit)
            item = {'case':name,'status':result.status,'error':result.error,'result':result.result,
                'elapsed_s':round(time.monotonic()-started,2),'events':events,'configuration':adapter.configuration()}
            if result.session:
                sessions[name] = result.session.to_dict()
            results.append(item)
            (base/(args.phase+'-results.json')).write_text(json.dumps({'cases':results,'sessions':sessions},ensure_ascii=False,indent=2).replace(key,'<redacted>'),encoding='utf-8')
            print(json.dumps({'case':name,'status':result.status,'error':result.error,'result':result.result[:500]},ensure_ascii=False),flush=True)
            return item,result
        finally:
            await adapter.close()

    def checked(item, checks):
        item['checks'] = checks
        item['passed'] = item['status']=='done' and all(checks.values())
        print(json.dumps({'case':item['case'],'passed':item['passed'],'checks':checks}),flush=True)

    def used(item, needle):
        return any(needle in json.dumps(e['payload'],ensure_ascii=False) for e in item['events'] if e['type']=='tool.result' and e['payload'].get('success'))

    if args.phase == 'native':
        item,_ = await run('native-handoff','根据 facts.json 整理简洁工作交接报告，保存为 native-handoff.md。')
        body = (work/'native-handoff.md').read_text(encoding='utf-8') if (work/'native-handoff.md').exists() else ''
        checked(item,{'native_skill_used':any(e['type']=='tool.result' and e['payload'].get('name')=='skill' and e['payload'].get('success') for e in item['events']),
            'artifact':'handoff-layout-v1' in body,'no_skill_lookup_failure':not any(e['type']=='tool.result' and e['payload'].get('name')=='skill' and not e['payload'].get('success') for e in item['events'])})
        item,_ = await run('native-csv','检查 orders.csv 的数据质量，将每项问题及数据行号保存为 native-audit.json，不修改原始 CSV。')
        try:
            audit = json.loads((work/'native-audit.json').read_text(encoding='utf-8'))
            issues = {(i['row'],i['code'],i['column']) for i in audit['issues']}
        except (OSError,ValueError,KeyError,TypeError):
            audit,issues = {},set()
        checked(item,{'native_skill_used':any(e['type']=='tool.result' and e['payload'].get('name')=='skill' and e['payload'].get('success') for e in item['events']),
            'artifact':audit.get('row_count')==5 and audit.get('rules_revision')=='csv-audit-r1' and issues=={(2,'negative_amount','amount'),(3,'duplicate_id','id'),(4,'missing_value','id'),(5,'missing_value','amount')},
            'source_unchanged':(work/'orders.csv').read_text(encoding='utf-8')=='id,amount\nA,10\nB,-3\nA,5\n,7\nC,\n'})
    elif args.phase == 'prepare':
        item,handoff = await run('handoff','根据 facts.json 整理简洁的工作交接报告，保存为 handoff.md。只使用给定事实。')
        body = (work/'handoff.md').read_text(encoding='utf-8') if (work/'handoff.md').exists() else ''
        checked(item,{'skill_read':used(item,'SKILL.md'),'reference_read':used(item,'layout.json'),'artifact':all(s in body for s in ['handoff-layout-v1','Added CSV import','Human review'])})
        item,_ = await run('csv','检查 orders.csv 的数据质量，将每项问题及数据行号保存为 audit.json，不修改原始 CSV。')
        try:
            audit = json.loads((work/'audit.json').read_text(encoding='utf-8'))
            issues = {(i['row'],i['code'],i['column']) for i in audit['issues']}
        except (OSError,ValueError,KeyError,TypeError):
            audit,issues = {},set()
        checked(item,{'skill_read':used(item,'csv-audit'),'reference_read':used(item,'rules.json'),
            'artifact':audit.get('row_count')==5 and audit.get('rules_revision')=='csv-audit-r1' and issues=={(2,'negative_amount','amount'),(3,'duplicate_id','id'),(4,'missing_value','id'),(5,'missing_value','amount')}})
        item,_ = await run('notes','在连接的笔记服务中保存名为 plugin-live 的笔记，内容为 SYNTHETIC-NOTE-6c2e，然后通过笔记服务读回并确认。')
        note = next(Path(r['data_path'])/'notes/plugin-live.txt' for r in records if r['id']=='com.example.notes-mcp')
        checked(item,{'save_call':used(item,'save_note'),'read_call':used(item,'read_note'),'persisted':note.exists() and note.read_text(encoding='utf-8')=='SYNTHETIC-NOTE-6c2e'})
        item,_ = await run('filesystem','通过连接的 archive 文件服务寻找 catalog.json，读取并告诉我 marker 和 items，不要用本地 shell。')
        checked(item,{'service_used':used(item,'archive'),'content':'catalog-7f3c91' in item['result'] and 'notebook' in item['result']})
        item,_ = await run('web','实际获取 https://example.com/ 并告诉我页面主标题。不要凭记忆回答，不要使用 shell。')
        checked(item,{'fetch_used':used(item,'example.com'),'content':'Example Domain' in item['result']})
        if handoff.session:
            item,_ = await run('resume','不要读取文件，只根据本会话之前内容回答：报告末尾的模板版本标记是什么？',session=ProviderSessionHandle.from_dict(handoff.session.to_dict()))
            checked(item,{'history':'handoff-layout-v1' in item['result'],'no_tool_calls':not any(e['type']=='tool.call' for e in item['events'])})
    elif args.phase == 'update':
        saved = json.loads((base/'prepare-results.json').read_text(encoding='utf-8'))['sessions']['handoff']
        item,_ = await run('old-session-after-update','继续之前的报告。',session=ProviderSessionHandle.from_dict(saved))
        item['passed'] = item['status']=='error' and 'Skills changed' in (item['error'] or '') and not item['events']
        item,_ = await run('updated-handoff','根据 facts.json 整理简洁交接报告，保存为 handoff-v2.md。')
        body = (work/'handoff-v2.md').read_text(encoding='utf-8') if (work/'handoff-v2.md').exists() else ''
        checked(item,{'new_reference':used(item,'layout.json'),'new_version':'handoff-layout-v2' in body,'old_version_absent':'handoff-layout-v1' not in body})
    elif args.phase == 'persistence':
        item,_ = await run('notes-after-restart','通过连接的笔记服务读回名为 plugin-live 的笔记，告诉我其内容，不要写入或修改。')
        checked(item,{'read_call':used(item,'read_note'),'persisted_across_processes':'SYNTHETIC-NOTE-6c2e' in item['result']})
    else:
        if args.phase == 'remove':
            saved = json.loads((base/'update-results.json').read_text(encoding='utf-8'))['sessions']['updated-handoff']
            item,_ = await run('old-session-after-uninstall','继续之前的报告。',session=ProviderSessionHandle.from_dict(saved))
            item['passed'] = item['status']=='error' and 'Skills changed' in (item['error'] or '') and not item['events']
            item,_ = await run('after-uninstall','检查当前是否有可用的 Amadeus Skill 和笔记服务。如没有请明确说明，不要用普通文件操作模仿这些能力。')
            checked(item,{'no_skill_catalog':not records,'no_removed_tool':not any('save_note' in str(e['payload']) or 'read_note' in str(e['payload']) for e in item['events'])})
        else:
            item,_ = await run('native-web-disabled','检查当前工具是否提供 web_fetch。只返回 JSON 对象，字段 web_fetch_available 为布尔值。不要用 shell 或其他手段替代。')
            try:
                availability = json.loads(item['result'].strip().removeprefix('```json').removesuffix('```').strip())
            except ValueError:
                availability = {}
            checked(item,{'no_fetch':not any('web_fetch' in str(e['payload'].get('name','')) for e in item['events'] if e['type']=='tool.call'),'unavailable':availability.get('web_fetch_available') is False})
    await asyncio.sleep(1)
    remaining = []
    for pid,created in children.items():
        try:
            if psutil.Process(pid).create_time()==created:
                remaining.append(pid)
        except psutil.NoSuchProcess:
            pass
    summary = {'cases':results,'sessions':sessions,'remaining_children':remaining}
    (base/(args.phase+'-results.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2).replace(key,'<redacted>'),encoding='utf-8')
    print(json.dumps({'phase':args.phase,'passed':all(i.get('passed') for i in results),'remaining_children':remaining}),flush=True)
    configure_skills(SkillCatalog())
    return 0 if all(i.get('passed') for i in results) and not remaining else 1

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
