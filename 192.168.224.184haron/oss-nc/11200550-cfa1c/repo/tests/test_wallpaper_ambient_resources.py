"""Replayed scene assets retain incoming textures and release replaced assets."""
from pathlib import Path
import subprocess
import shutil

import pytest

ROOT=Path(__file__).resolve().parents[1]


def test_ambient_replay_texture_ownership():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('render/web/wallpaper_scene.js','utf8');
const extract=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end)).trim().replace(/,$/,'');
class Sprite{constructor(texture){this.texture=texture;}destroy(options){if(options.texture)this.texture.destroy(options.baseTexture);}}
const methods=vm.runInNewContext('({'+extract('_replaceAmbientLowSprite(texture) {','_loadAmbientLowSprite(url) {')+','+
 extract('_replaceAmbientSprite(texture, sourceKind) {','_loadAmbientSprite(url, sourceKind) {')+'})',{PIXI:{Sprite,BLEND_MODES:{}},console:{info(){}}});
const makeTexture=(base={destroyed:false})=>({baseTexture:base,destroyed:false,destroy(destroyBase){this.destroyed=true;if(destroyBase)this.baseTexture.destroyed=true;}});
for(const [method,property] of [['_replaceAmbientLowSprite','ambientLowSprite'],['_replaceAmbientSprite','ambientSprite']]){
 for(const mode of ['same-texture','different-texture','shared-base']){
  const previous=makeTexture(),incoming=mode==='same-texture'?previous:makeTexture(mode==='shared-base'?previous.baseTexture:undefined);
  const owner={app:{},ambientLayer:{removeChild(){},addChild(){},addChildAt(){}},_placeBackdrop(){},
   _ambientLowIdleAlpha:()=>1,_ambientBaseAlpha:()=>1,_drawGlow(){},[property]:new Sprite(previous)};
  methods[method].call(owner,incoming,'delta');
  assert.strictEqual(owner[property].texture,incoming);
  assert.equal(incoming.destroyed,false);assert.equal(incoming.baseTexture.destroyed,false);
  assert.equal(previous.destroyed,mode!=='same-texture');
  if(mode==='different-texture')assert.equal(previous.baseTexture.destroyed,true);
 }
}
'''
    subprocess.run([node,"-e",script],cwd=ROOT,check=True,capture_output=True,text=True,timeout=10)
