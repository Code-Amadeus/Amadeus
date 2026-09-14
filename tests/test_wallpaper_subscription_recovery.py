"""A connected renderer receives the Host state it missed while disconnected."""
import json
from pathlib import Path
import subprocess

from wallpaper.wallpaper_engine_bridge import _BridgeState


def drain(client):
    result = []
    while not client.empty():
        result.append(client.get_nowait())
    return result


def test_wallpaper_subscription_starts_with_current_state_then_live_updates():
    state = _BridgeState()
    bootstrap = {"method":"initDesktopScene", "args":[{"enabled":True}]}
    work = {"method":"setActivity", "args":["work"]}
    idle = {"method":"setActivity", "args":["idle"]}
    state.add_bootstrap(bootstrap, key="desktop")
    state.publish(work, replay="activity")
    client = state.add_client()
    assert drain(client) == [bootstrap, work]
    state.publish(idle, replay="activity")
    assert drain(client) == [idle]

    state.remove_client(client)
    state.publish(work, replay="activity")
    reconnected = state.add_client()
    assert drain(reconnected) == [bootstrap, work]
    assert client.empty()


def test_reconnected_wallpaper_does_not_replay_transient_animation_commands():
    state = _BridgeState()
    state.publish({"method":"playSpriteForgeNode", "args":["old-once"]})
    idle = {"method":"setActivity", "args":["idle"]}
    state.publish(idle, replay="activity")
    assert drain(state.add_client()) == [idle]


def test_slice_subscription_replays_only_its_current_presentation():
    state = _BridgeState()
    state.publish({"method":"setActivity", "args":["work"]}, replay="activity")
    canvas = {"method":"setCanvas", "args":[{"title":"Approval required"}]}
    state.publish(canvas, replay="canvas")
    client = state.add_canvas_client()
    assert drain(client) == [canvas]
    state.remove_canvas_client(client)
    done = {"method":"setCanvas", "args":[{"title":"Export complete"}]}
    state.publish(done, replay="canvas")
    assert drain(state.add_canvas_client()) == [done]


def test_slice_refreshes_bridge_credential_after_reconnection():
    root = Path(__file__).resolve().parents[1]
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('render/web/electron_slice_host.js', 'utf8');
const fragment = source.slice(source.indexOf('  async function resolveBridge()'), source.indexOf('  async function start()'));
let token = 'first', reads = 0;
const sockets = [];
const context = {
  window: {location:{origin:'http://127.0.0.1:17778'}},
  fetch: async () => { reads++; return {ok:true,json:async()=>({bridgePort:17797,bridgeToken:token})}; },
  normalizePort: value => Number(value), bridgePort:17797, eventSource:null,
  bridgeEndpoint: path => 'http://127.0.0.1:17797/wallpaper/'+path,
  applyCall: () => {}, console:{warn:()=>{}},
  EventSource: class { constructor() {sockets.push(this);} close() {} },
};
vm.createContext(context);
vm.runInContext(fragment, context);
(async()=>{
 await context.resolveBridge(); context.connectCanvasEvents();
 const socket = sockets[0]; if(socket.onopen) await socket.onopen();
 const initialReads=reads;
 if(socket.onerror) socket.onerror();
 token='second'; if(socket.onopen) await socket.onopen();
 process.stdout.write(JSON.stringify({initialReads,reads,token:context.window.__amadeusBridgeToken}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result = subprocess.run(["node", "-e", script], cwd=root,
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=10)
    assert json.loads(result.stdout) == {"initialReads":1, "reads":2, "token":"second"}
