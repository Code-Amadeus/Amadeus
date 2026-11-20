"""Semantic adapter boundaries which do not require the licensed Cubism runtime."""
from pathlib import Path
import subprocess
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_visible_sdk_geometry_frames_stably_across_aspect_ratios():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    script = r'''
const assert=require('node:assert/strict');
const {Live2DCharacter}=require('./render/web/live2d_character.js');
const opacity=[1,0,1];
const vertices=[[10,20,30,40],[-1000,-1000,1000,1000],[40,50,70,80]];
const internal={width:16384,height:32,coreModel:{getDrawableCount:()=>3,getDrawableOpacity:i=>opacity[i],
 getDrawableVertices:()=>{throw new Error('Raw Core units are not artwork pixel coordinates');}},
 getDrawableVertices:i=>vertices[i],localTransform:{a:2,b:0,c:0,d:3,tx:100,ty:200}};
const adapter=Object.create(Live2DCharacter.prototype);
adapter.model={internalModel:internal,scale:{set:s=>adapter.s=s},
 position:{set:(x,y)=>{adapter.x=x;adapter.y=y;}}};
adapter.artBounds=adapter._measureArtBounds();
assert.deepEqual(adapter.artBounds,{minX:120,minY:260,maxX:240,maxY:440,width:120,height:180});
global.PIXI={RenderTexture:{create:options=>({...options,baseTexture:{resolution:options.resolution},destroy(){}})}};
adapter.app={renderer:{resolution:1}};adapter.config={layout:{scale:.9,x:0,y:0}};
adapter.display={position:{set(){}},texture:null};
for(const viewport of [{x:0,y:0,width:360,height:720},{x:0,y:0,width:900,height:900}]){
 internal.width=viewport.height;internal.height=viewport.width*30;
 adapter.setViewport(viewport);
 const left=adapter.x+adapter.artBounds.minX*adapter.s;
 const right=adapter.x+adapter.artBounds.maxX*adapter.s;
 const bottom=adapter.y+adapter.artBounds.maxY*adapter.s;
 const top=adapter.y+adapter.artBounds.minY*adapter.s;
 assert.ok(left>=0&&right<=viewport.width&&top>=0&&bottom<=viewport.height+1e-9);
 const occupancy=Math.max((right-left)/viewport.width,(bottom-top)/viewport.height);
 assert.ok(Math.abs(occupancy-.9)<1e-9);
 assert.ok(Math.abs(left-(viewport.width-right))<1e-9);
 assert.ok(Math.abs(bottom-viewport.height)<1e-9);
}
// Animated geometry changes do not cause framing jitter on a later resize.
vertices[0]=[-500,-500,500,500];const stable=adapter.artBounds;
adapter.setViewport({x:0,y:0,width:600,height:800});assert.strictEqual(adapter.artBounds,stable);
adapter.config.layout.x=.1;adapter.config.layout.y=-.1;adapter._layout();
assert.ok(Math.abs(adapter.x+(stable.minX+stable.maxX)*.5*adapter.s-360)<1e-9);
assert.ok(Math.abs(adapter.y+stable.maxY*adapter.s-720)<1e-9);
'''
    subprocess.run([node, "-e", script], cwd=ROOT, check=True,
                   capture_output=True, text=True, timeout=10)
