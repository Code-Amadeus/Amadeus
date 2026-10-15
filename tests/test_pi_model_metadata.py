"""只修补当前选择；用原生 SDK 离线验证目录、声明、认证及图像转换。"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "agent_host/pi_runtime"
pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not (RUNTIME / "node_modules/@earendil-works/pi-ai").is_dir(),
    reason="optional pinned Pi runtime not installed",
)

COMMON = r'''
import assert from 'node:assert/strict';
import {registerHostModelMetadata} from './model-metadata.ts';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import path from 'node:path';
import { ModelRuntime, ModelRegistry, DefaultResourceLoader, SettingsManager,
  SessionManager, createAgentSession, resolveCliModel } from '@earendil-works/pi-coding-agent';
import { deepseekProvider } from '@earendil-works/pi-ai/providers/deepseek';
import { convertMessages } from '@earendil-works/pi-ai/api/openai-completions';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai/utils/event-stream';

// 仅使用公开包导出；所有配置、凭据和目录均为离线合成夹具。
const outputDir = FIXTURE_DIR;
let fetchAttempts = 0;
globalThis.fetch = () => { fetchAttempts++; throw new Error('offline_network_forbidden'); };
const aliases = new Set(['deepseek-v4-flash', 'deepseek-v4-flash-vision-exp']);
const png = {type: 'image', mimeType: 'image/png', data:
  'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAJklEQVR4nO3NMQ0AAAwDoPo33arYsQQMkB6LQCAQCAQCgUAg+BIMi1X0pjxKe0gAAAAASUVORK5CYII='};
const base = deepseekProvider().getModels().find(m => m.id === 'deepseek-flash');
const results = [];

function memoryStore(entry) {
  const entries = new Map(entry ? [['deepseek', structuredClone(entry)]] : []);
  return { async read(id) { return structuredClone(entries.get(id)); },
    async write(id, value) { entries.set(id, structuredClone(value)); },
    async delete(id) { entries.delete(id); } };
}
function credentials(configured) {
  const entries = new Map(configured ? [['deepseek', {type:'api_key',key:'SYNTHETIC_OFFLINE_PROBE'}]] : []);
  return { async read(id) { return entries.get(id); },
    async list() { return [...entries].map(([providerId, v]) => ({providerId,type:v.type})); },
    async modify(id, fn) { const v = await fn(entries.get(id));
      if (v) entries.set(id,v); else entries.delete(id); return v; },
    async delete(id) { entries.delete(id); } };
}
function outgoing(model) {
  const converted = convertMessages(model, {messages: [
    {role:'assistant',api:model.api,provider:model.provider,model:model.id,
      content:[{type:'toolCall',id:'image-fixture',name:'instant_image',arguments:{}}],
      stopReason:'toolUse',timestamp:1},
    {role:'toolResult',toolCallId:'image-fixture',toolName:'instant_image',
      content:[png],isError:false,timestamp:2},
  ]}, {supportsDeveloperRole:false,supportsMidConvoSystemMessages:false,
    requiresReasoningContentOnAssistantMessages:true});
  return converted.flatMap(m => Array.isArray(m.content) ? m.content : [])
    .filter(x => x.type === 'image_url').length;
}
function repairFactory(events) {
  return pi => { registerHostModelMetadata(pi);
    pi.on('model_select',e=>{events.push({id:e.model.id})}); };
}
async function setup(name, {id='deepseek-v4-flash', config, cache, native, extension, configured=true, fullExtension=false}={}) {
  const directory = path.join(outputDir,name);
  await mkdir(directory);
  process.env.PI_CODING_AGENT_DIR=directory;
  process.env.AMADEUS_PI_WORKSPACE=directory;
  process.env.AMADEUS_PI_ACCESS='none';
  let modelsPath = path.join(directory,'models.json');
  if (config) {
    modelsPath = path.join(directory,'models.json');
    await writeFile(modelsPath,typeof config==='string' ? config : JSON.stringify(config,null,2)+'\n','utf8');
  }
  const store = memoryStore(cache);
  const runtime = await ModelRuntime.create({credentials:credentials(configured),modelsPath,modelsStore:store,
    refreshOnCreate:true,allowModelNetwork:false});
  if (native) runtime.registerNativeProvider(native);
  if (extension) runtime.registerProvider('deepseek',extension);
  const registry = new ModelRegistry(runtime);
  const selected = resolveCliModel({cliProvider:'deepseek',cliModel:id,modelRuntime:runtime});
  assert.equal(selected.error,undefined);
  const settings = SettingsManager.inMemory({defaultProvider:'deepseek',defaultModel:'deepseek-flash',
    compaction:{enabled:false},cacheWarming:{mode:'off'}});
  const events = [];
  const loader = new DefaultResourceLoader({cwd:directory,agentDir:directory,settingsManager:settings,
    noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,
    disabledBuiltinExtensions:['mcp'], ...(fullExtension ? {additionalExtensionPaths:[path.join(process.cwd(),'amadeus.ts')]} : {extensionFactories:[{name:'offline-alias-probe',factory:repairFactory(events)}]})});
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors,[]);
  const {session} = await createAgentSession({cwd:directory,agentDir:directory,modelRuntime:runtime,
    model:selected.model,settingsManager:settings,sessionManager:SessionManager.inMemory(directory),
    resourceLoader:loader,noTools:'all'});
  const providerBefore = runtime.getProvider('deepseek');
  const errors=[];
  await session.bindExtensions({mode:'rpc',onError:e=>errors.push(e.error)});
  assert.strictEqual(runtime.getProvider('deepseek'),providerBefore);
  const requests = [];
  // 公开 Agent.streamFunction 注入合成终态，保留真实 Session/事件/转换路径。
  session.agent.streamFunction = (model,context) => {
    const converted = convertMessages(model,context,{supportsDeveloperRole:false,
      supportsMidConvoSystemMessages:false,requiresReasoningContentOnAssistantMessages:true});
    requests.push({id:model.id,input:[...model.input],contextWindow:model.contextWindow,
      images:converted.flatMap(m=>Array.isArray(m.content)?m.content:[]).filter(x=>x.type==='image_url').length});
    const stream = createAssistantMessageEventStream();
    const message = {role:'assistant',content:[{type:'text',text:'offline probe complete'}],
      api:model.api,provider:model.provider,model:model.id,
      usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
        cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'stop',timestamp:Date.now()};
    queueMicrotask(()=>{stream.push({type:'done',reason:'stop',message});stream.end(message);});
    return stream;
  };
  return {runtime,registry,store,session,events,requests,settings,modelsPath,selected,errors};
}

'''

CASES = {
    'deepseek-v4-flash': r'''
const s=await setup('fixture',{id:'deepseek-v4-flash'});
try {
  assert.ok(s.selected.warning);
  assert.deepEqual(s.selected.model.input,['text']);
  assert.equal(s.registry.find('deepseek','deepseek-v4-flash'),undefined);
  assert.equal(s.registry.getRegisteredNativeProvider('deepseek'),undefined);
  assert.equal(s.registry.getRegisteredProviderConfig('deepseek'),undefined);
  assert.deepEqual({...s.session.model,id:base.id},s.registry.find('deepseek',base.id));
  assert.equal(outgoing(s.session.model),1);
  await s.session.prompt('offline image',{images:[png]});
  assert.equal(s.requests[0].images,1);
  assert.equal(s.requests[0].id,'deepseek-v4-flash');
  assert.deepEqual(s.events,[]);
  assert.deepEqual(s.errors,[]);
  assert.equal(s.settings.getSettings().defaultModel,'deepseek-flash');
} finally {s.session.dispose();}
''',
    'deepseek-v4-flash-vision-exp': r'''
const s=await setup('fixture',{id:'deepseek-v4-flash-vision-exp'});
try {
  assert.ok(s.selected.warning);
  assert.deepEqual(s.selected.model.input,['text']);
  assert.equal(s.registry.find('deepseek','deepseek-v4-flash-vision-exp'),undefined);
  assert.equal(s.registry.getRegisteredNativeProvider('deepseek'),undefined);
  assert.equal(s.registry.getRegisteredProviderConfig('deepseek'),undefined);
  assert.deepEqual({...s.session.model,id:base.id},s.registry.find('deepseek',base.id));
  assert.equal(outgoing(s.session.model),1);
  await s.session.prompt('offline image',{images:[png]});
  assert.equal(s.requests[0].images,1);
  assert.equal(s.requests[0].id,'deepseek-v4-flash-vision-exp');
  assert.deepEqual(s.events,[]);
  assert.deepEqual(s.errors,[]);
  assert.equal(s.settings.getSettings().defaultModel,'deepseek-flash');
} finally {s.session.dispose();}
''',
    'deepseek-v4-pro': r'''
const s=await setup('fixture',{id:'deepseek-v4-pro'});
try {
  assert.strictEqual(s.session.model,s.selected.model);
  assert.deepEqual(s.session.model.input,['text']);
  assert.equal(outgoing(s.session.model),0);
} finally {s.session.dispose();}
''',
    'unknown-future-model': r'''
const s=await setup('fixture',{id:'unknown-future-model'});
try {
  assert.strictEqual(s.session.model,s.selected.model);
  assert.deepEqual(s.session.model.input,['text']);
  assert.equal(outgoing(s.session.model),0);
} finally {s.session.dispose();}
''',
    'dynamic-cache-and-native-refresh': r'''
const canonical={...structuredClone(base),contextWindow:base.contextWindow+1234};
const extra={...structuredClone(base),id:'fixture-cached-only'};
const s=await setup('fixture',{cache:{models:[canonical,extra],lastModified:Date.now()+86400000}});
try {
  assert.equal(s.session.model.contextWindow,canonical.contextWindow);
  assert.ok(s.registry.find('deepseek',extra.id));
  const next={...canonical,contextWindow:canonical.contextWindow+1,input:['text']};
  await s.store.write('deepseek',{models:[next,extra],lastModified:Date.now()+172800000});
  await s.runtime.refresh({allowNetwork:false});
  const provider=s.runtime.getProvider('deepseek');
  await s.session.prompt('offline next turn');
  assert.equal(s.requests[0].contextWindow,next.contextWindow);
  assert.deepEqual(s.requests[0].input,['text']);
  assert.strictEqual(s.runtime.getProvider('deepseek'),provider);
  assert.ok(s.registry.find('deepseek',extra.id));
} finally {s.session.dispose();}
''',
    'explicit-alias-models': r'''
const config={providers:{deepseek:{models:[{id:'deepseek-v4-flash',api:base.api,input:['text'],contextWindow:12345,maxTokens:256}]}}};
const s=await setup('fixture',{config});
try {
  assert.strictEqual(s.session.model,s.selected.model);
  assert.deepEqual(s.session.model.input,['text']);
  assert.equal(outgoing(s.session.model),0);
  assert.deepEqual(JSON.parse(await readFile(s.modelsPath,'utf8')),config);
  assert.deepEqual(s.errors,[]);
} finally {s.session.dispose();}
''',
    'explicit-alias-modelOverrides': r'''
const config={providers:{deepseek:{modelOverrides:{'deepseek-v4-flash':{input:['text']}}}}};
const s=await setup('fixture',{config});
try {
  assert.strictEqual(s.session.model,s.selected.model);
  assert.deepEqual(s.session.model.input,['text']);
  assert.equal(outgoing(s.session.model),0);
  assert.deepEqual(JSON.parse(await readFile(s.modelsPath,'utf8')),config);
  assert.deepEqual(s.errors,[]);
} finally {s.session.dispose();}
''',
    'canonical-endpoint-overrides-auth': r'''
const config={providers:{deepseek:{baseUrl:'https://offline.invalid/v1',
  modelOverrides:{'deepseek-flash':{contextWindow:654321,compat:{supportsStore:true}}}}}};
const s=await setup('fixture',{config});
try {
  const canonical=s.registry.find('deepseek','deepseek-flash');
  assert.deepEqual({...s.session.model,id:canonical.id},canonical);
  assert.equal(s.session.model.baseUrl,'https://offline.invalid/v1');
  assert.equal(s.session.model.contextWindow,654321);
  assert.equal(s.session.model.compat.supportsStore,true);
  assert.equal((await s.runtime.getAuth(s.session.model)).auth.apiKey,'SYNTHETIC_OFFLINE_PROBE');
  assert.deepEqual(JSON.parse(await readFile(s.modelsPath,'utf8')),config);
} finally {s.session.dispose();}
''',
    'native-provider-registration': r'''
const native={...deepseekProvider(),getModels:()=>[{...structuredClone(base),input:['text']}]};
const s=await setup('fixture',{native});
try {assert.strictEqual(s.session.model,s.selected.model);assert.equal(outgoing(s.session.model),0);}
finally {s.session.dispose();}
''',
    'no-auth': r'''
const s=await setup('fixture',{configured:false});
try {assert.strictEqual(s.session.model,s.selected.model);assert.equal(outgoing(s.session.model),0);
  assert.deepEqual(s.errors,[]);}
finally {s.session.dispose();}
''',
    'extension-provider-registration': r'''
const extension={models:[{...structuredClone(base),input:['text']}],baseUrl:'https://offline.invalid/v1'};
const s=await setup('fixture',{extension});
try {assert.strictEqual(s.session.model,s.selected.model);assert.equal(outgoing(s.session.model),0);
  assert.ok(s.registry.getRegisteredProviderConfig('deepseek'));}
finally {s.session.dispose();}
''',
    'canonical-text-only-override': r'''
const config={providers:{deepseek:{modelOverrides:{'deepseek-flash':{input:['text']}}}}};
const s=await setup('fixture',{config});
try {assert.deepEqual({...s.session.model,id:base.id},s.registry.find('deepseek',base.id));
  assert.equal(outgoing(s.session.model),0);}
finally {s.session.dispose();}
''',
    'bad-json': r'''
const config='{"providers":{"deepseek":{"apiKey":"SYNTHETIC_SECRET_MARKER"}},';
const s=await setup('fixture',{config});
try {
  assert.deepEqual(s.session.model.input,['text']);
  assert.ok(s.errors.some(e=>e==='Amadeus model metadata: native models.json declarations could not be read'));
  assert.ok(s.errors.every(e=>!e.includes('SYNTHETIC_SECRET_MARKER')));
  assert.equal(await readFile(s.modelsPath,'utf8'),config);
} finally {s.session.dispose();}
''',
    'native-rejects-block-comment': r'''
const config='{"providers":{/* unsupported native comment */"deepseek":{"modelOverrides":{}}}}';
const s=await setup('fixture',{config});
try {
  assert.deepEqual(s.session.model.input,['text']);
  assert.ok(s.errors.some(e=>e==='Amadeus model metadata: native models.json declarations could not be read'));
  assert.ok(s.errors.every(e=>!e.includes('SYNTHETIC_SECRET_MARKER')));
  assert.equal(await readFile(s.modelsPath,'utf8'),config);
} finally {s.session.dispose();}
''',
    'native-jsonc-bom': r'''
const config='\ufeff{ // comment\n "providers": { "deepseek": { "modelOverrides": {"deepseek-v4-flash":{"input":["text"],},}, }, }, }';
const s=await setup('fixture',{config});
try {assert.equal(s.runtime.getError(),undefined);assert.equal(outgoing(s.session.model),0);
  assert.deepEqual(s.errors,[]);assert.equal(await readFile(s.modelsPath,'utf8'),config);}
finally {s.session.dispose();}
''',
    'full-host-extension': r'''
const s=await setup('fixture',{fullExtension:true});
try {assert.ok(s.selected.warning);assert.equal(outgoing(s.session.model),1);
  assert.equal(s.registry.find('deepseek','deepseek-v4-flash'),undefined);assert.deepEqual(s.errors,[]);}
finally {s.session.dispose();}
''',
    'declaration-snapshot-new-work': r'''
const s=await setup('first-work');
const config={providers:{deepseek:{modelOverrides:{'deepseek-v4-flash':{input:['text']}}}}};
try {
  await writeFile(s.modelsPath,JSON.stringify(config),'utf8');
  await s.session.prompt('offline same Work');
  assert.equal(outgoing(s.session.model),1);
} finally {s.session.dispose();}
const next=await setup('new-work',{config});
try {assert.equal(outgoing(next.session.model),0);assert.deepEqual(next.errors,[]);}
finally {next.session.dispose();}
''',
}


@pytest.mark.parametrize("case", CASES)
def test_native_session_model_metadata(tmp_path, case):
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    environment.update(PI_OFFLINE="1", PI_TELEMETRY="0", PI_SKIP_VERSION_CHECK="1")
    source = COMMON.replace("FIXTURE_DIR", json.dumps(str(tmp_path))) + CASES[case]
    source += "\nassert.equal(fetchAttempts,0);console.log(JSON.stringify({passed:true}));"
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", source],
                            cwd=RUNTIME, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"passed": True}
