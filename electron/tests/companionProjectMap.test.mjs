import test from 'node:test'
import assert from 'node:assert/strict'
import { projectOverviewMap, projectMiniature } from '../src/renderer/components/companionProjectMap.ts'
import { taskBranchPath } from '../src/renderer/components/companionConstellationLayout.ts'
const character={x:257,y:750,width:518,height:1070}
const rail={x:868,y:66,width:164,height:450}
const members=count=>Array.from({length:count},(_,i)=>({id:`task-${i}`,...(i>=5?{parentTaskId:`task-${(i-5)%5}`}:{})}))
const box=(node,scale)=>({...node,width:node.width*scale,height:node.height*scale})
const overlap=(a,b)=>a.x<b.x+b.width&&b.x<a.x+a.width&&a.y<b.y+b.height&&b.y<a.y+a.height

test('five main conversations and four side conversations are readable together, without page, pile or face overlap',()=>{
  const input=members(9), result=projectOverviewMap(input,1032,1820,[rail],character)
  assert.equal(result.scale,1)
  assert.equal(result.nodes.length,9)
  const protectedCore={x:309,y:810,width:414,height:1010}
  for(const [i,node] of result.nodes.entries()) {
    assert.ok(node.width>=220 && node.height>=160)
    assert.ok(node.x>=0 && node.x+node.width<=1032 && node.y>=0 && node.y+node.height<=1820)
    assert.equal(overlap(node,protectedCore),false)
    assert.equal(overlap(node,rail),false)
    for(const other of result.nodes.slice(i+1)) assert.equal(overlap(node,other),false)
    const parentId=input.find(member=>member.id===node.id).parentTaskId
    if(parentId) {
      const parent=result.nodes.find(member=>member.id===parentId)
      assert.ok(node.y>=parent.y+parent.height)
      assert.ok(Math.abs(node.x-parent.x)<40)
    }
  }
  assert.ok(result.borrowed<=character.width*character.height*.12)
  assert.deepEqual(projectOverviewMap([...input].reverse(),1032,1820,[rail],character),result,'Membership order does not shuffle family anchors')
})

test('overflow keeps every identity in one fitted map inside the available area, rather than silently losing tasks',()=>{
  for(const count of [15,25,50,100]) {
    const input=members(count),result=projectOverviewMap(input,1032,1820,[rail],character)
    assert.deepEqual(new Set(result.nodes.map(node=>node.id)),new Set(input.map(node=>node.id)))
    const boxes=result.nodes.map(node=>box(node,result.scale))
    for(const [i,node] of boxes.entries()) {
      assert.ok(node.x>=0 && node.x+node.width<=1032 && node.y>=0 && node.y+node.height<=1820)
      assert.equal(overlap(node,rail),false)
      for(const other of boxes.slice(i+1)) assert.equal(overlap(node,other),false)
    }
  }
})

test('miniature keeps every task and real parent, including beyond the previous four-card cap',()=>{
  const input=members(30),result=projectMiniature(input,140)
  assert.equal(result.nodes.length,30)
  for(const node of result.nodes) assert.ok(node.x>=0&&node.x+node.width<=140)
  const parent=result.nodes.find(node=>node.id==='task-0'),nextFamily=result.nodes.find(node=>node.id==='task-1')
  for(const child of input.filter(node=>node.parentTaskId==='task-0')) {
    const pose=result.nodes.find(node=>node.id===child.id)
    assert.ok(pose.y>parent.y && pose.y<nextFamily.y)
  }
})

test('a character on a different display cannot consume this display project space',()=>{
  const input=members(25)
  assert.deepEqual(projectOverviewMap(input,1032,1820,[],{...character,x:3000}),projectOverviewMap(input,1032,1820,[]))
})

test('a second side conversation connects around its sibling instead of drawing a misleading chain through it',()=>{
  const parent={left:12,right:252,top:120,bottom:308}, first={left:30,right:270,top:332,bottom:520}, second={left:30,right:270,top:544,bottom:732}
  const direct=taskBranchPath(parent,first,[parent,first,second])
  assert.equal(direct,taskBranchPath(parent,first))
  const routed=taskBranchPath(parent,second,[parent,first,second])
  assert.notEqual(routed,taskBranchPath(parent,second))
  assert.match(routed,/L 282 /,'Route occupies the free family gutter beside the first sibling')
})
