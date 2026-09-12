import test from 'node:test'
import assert from 'node:assert/strict'
import { withCompanionContexts, groupCompanionTasks } from '../src/renderer/components/floatingCompanionState.ts'
import { CompanionNotificationQueue, TASK_INACTIVITY_MS } from '../src/renderer/components/companionNotificationQueue.ts'
import { constellationLayout, crossDisplayBranchPath, familyShape } from '../src/renderer/components/companionConstellationLayout.ts'
import { stackAttention, stackOrder } from '../src/renderer/components/companionStack.ts'
import { companionDragSelection, companionGatherPlacements, companionLinkSource, restackCompanionOverview, displayFocusLayout, composeDisplayFocus } from '../src/renderer/components/companionDisplayFocus.ts'
import { resolveCompanionDesktop } from '../src/main/windowPlacement.ts'

const main = {id:1,bounds:{x:0,y:0,width:2560,height:1440},workArea:{x:0,y:0,width:2560,height:1392}}
const side = {id:2,bounds:{x:2560,y:0,width:1080,height:1920},workArea:{x:2560,y:0,width:1080,height:1872}}
const desktop = resolveCompanionDesktop([main,side],main)
const task = (id, phase='ready') => ({id,title:id,detail:'Result',phase,key:id+':'+phase,provider:'Codex',codexThreadId:id,
  announce:false,repeatable:phase==='ready',projectId:'p',projectName:'Project',lastActivityAt:TASK_INACTIVITY_MS*2})

test('inactive parent is one silent context node while its visible child exists; real parent replaces it', () => {
  const parent=task('parent'), child={...task('child','attention'),parentTaskId:'parent',sourceKind:'sidechat',announce:true}
  const queue=new CompanionNotificationQueue()
  queue.update([child],child.lastActivityAt)
  const graph=withCompanionContexts([...queue.visible.values()],[parent])
  assert.equal(graph.length,2)
  assert.equal(graph.find(t=>t.id==='parent').contextOnly,true)
  assert.equal(graph.find(t=>t.id==='parent').detail,'')
  assert.equal(graph.find(t=>t.id==='parent').lastActivityAt,undefined)
  assert.equal(queue.take().id,'child')
  const real=withCompanionContexts([parent,child],[parent])
  assert.equal(real.find(t=>t.id==='parent'),parent)
  assert.equal(real.length,2)
  assert.deepEqual(withCompanionContexts([],[parent]),[])
  const view=constellationLayout(groupCompanionTasks(graph),1032,1820,750)
  assert.equal(view.projects[0].count,1,'A context node must not inflate actual task counts')
  assert.ok(view.cards.find(t=>t.id==='parent').height < 60)
  assert.ok(view.cards.find(t=>t.id==='child').y > view.cards.find(t=>t.id==='parent').y)
  const nested=familyShape([{id:'grand',contextOnly:true},{id:'parent',parentTaskId:'grand',contextOnly:true},
    {id:'child',parentTaskId:'parent'},{id:'next',parentTaskId:'child'}],300,'natural',true)
  const [grand,anchor,first,next]=nested.nodes
  assert.ok(anchor.y>=grand.y+42)
  assert.ok(first.y>=anchor.y+42)
  assert.equal(first.compactParent,true)
  assert.ok(next.y>=first.y+58,'A parent must remain visible above its own child')
})

test('two through seven layers stay visually distinct, with every task visible together when the project opens', () => {
  for (const count of [2,3,4,5,6,7,8,25]) {
    const groups=[{id:'p',title:'P',tasks:Array.from({length:count},(_,i)=>task('task-'+i))}]
    const small=constellationLayout(groups,440,800,420)
    const pile=small.cards.filter(c=>!c.hidden)
    assert.equal(pile.length,Math.min(7,count))
    const bottoms=pile.map(c=>c.y+c.height*c.scale).sort((a,b)=>a-b)
    for(let i=1;i<bottoms.length;i++) assert.ok(bottoms[i]-bottoms[i-1]>=12)
    const expanded=constellationLayout(groups,1032,1820,750,'p')
    const ids=new Set(expanded.cards.filter(card=>!card.hidden&&!card.depth).map(card=>card.id))
    assert.equal(ids.size,count)
  }
})

test('manual top survives normal updates; new unresolved attention takes priority; automatic top is stable within a priority', () => {
  const tasks=[task('a','running'),task('b','ready'),task('c','attention')], acknowledged=new Set()
  assert.equal(stackOrder(tasks)[0].id,'c')
  const manual={taskId:'a',attention:stackAttention(tasks,acknowledged),manual:true}
  assert.equal(stackOrder(tasks,manual)[0].id,'a')
  const edited=tasks.map(t=>({...t,detail:'Changed',lastActivityAt:t.lastActivityAt+20}))
  assert.equal(stackOrder(edited,manual)[0].id,'a')
  assert.equal(stackOrder(tasks,manual,new Set(['c:attention']))[0].id,'a','Resolving another request must preserve the manually selected card')
  assert.equal(stackOrder([...tasks,{...task('d','attention'),lastActivityAt:999999999}],manual)[0].id,'d')
  const pair=[task('a','running'),{...task('b','running'),lastActivityAt:999999999}]
  assert.equal(stackOrder(pair,{taskId:'a',attention:'',manual:false})[0].id,'a')
  assert.equal(stackOrder([task('a','running'),task('b','ready')],{taskId:'a',attention:'',manual:false})[0].id,'b')
})

test('project and parent drag carry only same-display descendants and never a detached card', () => {
  const groups=[{id:'p',title:'P',tasks:[task('parent'),{...task('child'),parentTaskId:'parent',sourceKind:'sidechat'},task('peer')]}]
  const auto=constellationLayout(groups,1032,1820,750)
  const split={...auto,cards:auto.cards.map(c=>c.id==='child'?{...c,x:-1600,y:200}:c)}
  assert.deepEqual([...companionDragSelection(split,groups,desktop,'projects','p')].sort(),['projects:p','tasks:parent','tasks:peer'])
  assert.deepEqual([...companionDragSelection(split,groups,desktop,'tasks','parent')],['tasks:parent'])
  assert.deepEqual([...companionDragSelection(split,groups,desktop,'tasks','child')],['tasks:child'])
})

test('switching a physically placed pile preserves its anchor and a detached layer becomes a full independent card', () => {
  const groups=[{id:'p',title:'P',tasks:Array.from({length:8},(_,i)=>task('task-'+i))}]
  const auto=constellationLayout(groups,440,800,420)
  const split={...auto,cards:auto.cards.map(c=>c.id==='task-7'?{...c,x:-1600,y:200}:c)}
  const before=restackCompanionOverview(split,groups,desktop)
  const detached=before.cards.find(c=>c.id==='task-7')
  assert.equal(detached.depth,0);assert.equal(detached.hidden,false);assert.equal(detached.stackId,undefined)
  const front=before.cards.find(c=>!c.depth&&c.stackId)
  const after=restackCompanionOverview(split,groups,desktop,{[front.stackId]:{taskId:'task-6',attention:'',manual:true}})
  const next=after.cards.find(c=>c.id==='task-6')
  assert.equal(next.depth,0);assert.equal(next.x,front.x);assert.equal(next.y,front.y)
  assert.deepEqual(after.cards.find(c=>c.id==='task-7'),detached)
})

test('detached reader shrinks the other local project while the remote parent and project remain unchanged', () => {
  const groups=[{id:'a',title:'A',tasks:[task('parent'),{...task('child'),sourceKind:'sidechat',parentTaskId:'parent'}]},
    {id:'b',title:'B',tasks:[task('other')]}]
  const auto=constellationLayout(groups,1032,1820,750)
  const split={...auto,projects:auto.projects.map(p=>p.id==='b'?{...p,x:-900,y:100}:p),
    cards:auto.cards.map(c=>c.id==='child'?{...c,x:-1800,y:120}:c.id==='other'?{...c,x:-900,y:200}:c)}
  const view=displayFocusLayout(split,groups,desktop,1,{trail:[{projectId:'a',taskId:'child'}]},'natural',112,1340)
  const result=composeDisplayFocus(split,[view])
  assert.equal(result.cards.find(c=>c.id==='other').mini,true)
  assert.deepEqual(result.cards.find(c=>c.id==='parent'),split.cards.find(c=>c.id==='parent'))
  assert.deepEqual(result.projects.find(p=>p.id==='a'),split.projects.find(p=>p.id==='a'))
  assert.equal(result.cards.length,3)
  assert.deepEqual(composeDisplayFocus(split,[]),split)
})

test('cross-display links have two separate segments, preserving the empty gap between displays', () => {
  const path=crossDisplayBranchPath({left:50,right:350,top:100,bottom:280},{left:1800,right:2100,top:220,bottom:400},
    {x:0,y:0,width:1000,height:800},{x:1500,y:0,width:1000,height:800})
  assert.equal((path.match(/M /g)||[]).length,2)
  assert.ok(!path.includes('NaN'))
})

test('folding keeps every real parent visible; only siblings share a pile, regardless of the selected front', () => {
  const tasks=[task('parent'),{...task('child','attention'),parentTaskId:'parent',sourceKind:'sidechat'},
    {...task('second'),parentTaskId:'parent',sourceKind:'sidechat'},task('other-main'),
    {...task('other-side'),parentTaskId:'other-main',sourceKind:'sidechat'}]
  const groups=[{id:'p',title:'P',tasks}]
  const view=constellationLayout(groups,440,800,420)
  const initial=restackCompanionOverview(view,groups,desktop)
  for(const parent of ['parent','other-main']) {
    const pose=initial.cards.find(card=>card.id===parent)
    assert.ok(!pose.depth&&!pose.hidden&&!pose.stackId&&pose.compactParent)
    assert.deepEqual(companionLinkSource(pose,tasks,initial.cards),{kind:'projects',id:'p'})
  }
  const front=initial.cards.find(card=>card.id==='child')
  for(const taskId of ['child','second']) {
    const selected=restackCompanionOverview(view,groups,desktop,{[front.stackId]:{taskId,attention:'child:attention',manual:true}})
    const pose=selected.cards.find(card=>card.id===taskId)
    assert.equal(pose.depth,0)
    assert.deepEqual(companionLinkSource(pose,tasks,selected.cards),{kind:'cards',id:'parent'})
    assert.deepEqual(companionLinkSource(selected.cards.find(card=>card.id==='other-side'),tasks,selected.cards),{kind:'cards',id:'other-main'})
  }
  const detached=restackCompanionOverview({...view,cards:view.cards.map(card=>card.id==='child'?{...card,x:-1600,y:200}:card)},groups,desktop)
  assert.deepEqual(companionLinkSource(detached.cards.find(card=>card.id==='child'),tasks,detached.cards),{kind:'cards',id:'parent'})
  assert.equal(detached.projects[0].count,5,'Compact parents are real tasks counted once')
})

test('explicit gather preserves the automatic family shape and avoids other projects and character', () => {
  const groups=[{id:'p',title:'P',tasks:[task('parent'),{...task('child'),parentTaskId:'parent',sourceKind:'sidechat'}]},
    {id:'q',title:'Q',tasks:[task('other')]}]
  const base=constellationLayout(groups,1032,1820,750)
  const split={...base,cards:base.cards.map(card=>card.id==='child'?{...card,x:-1500,y:180}:card)}
  const result=companionGatherPlacements(base,split,desktop,'p',{x:280,y:800,width:520,height:1000})
  assert.ok(result)
  assert.deepEqual([...result.keys()].sort(),['projects:p','tasks:child','tasks:parent'])
  const nodes=[...base.cards.filter(c=>c.projectId==='p').map(c=>({...c,kind:'tasks'})),{...base.projects.find(p=>p.id==='p'),kind:'projects'}]
  const obstacles=[...split.cards.filter(c=>c.projectId!=='p'),...split.projects.filter(p=>p.id!=='p')]
  for(const node of nodes) {
    const position=result.get(node.kind+':'+node.id)
    for(const obstacle of obstacles) assert.ok(position.x+node.width*position.scale<=obstacle.x+24 || position.x>=obstacle.x+24+obstacle.width*obstacle.scale
      || position.y+node.height*position.scale<=obstacle.y+28 || position.y>=obstacle.y+28+obstacle.height*obstacle.scale)
  }
  assert.equal(companionGatherPlacements(base,split,desktop,'p',{x:0,y:0,width:1080,height:1872}),null)
})

test('saved positions from a formerly combined parent/side pile do not hide the newly exposed parent', () => {
  const tasks=[task('parent'),{...task('child','attention'),parentTaskId:'parent',sourceKind:'sidechat'},
    {...task('sibling'),parentTaskId:'parent',sourceKind:'sidechat'},task('remote')]
  const groups=[{id:'p',title:'P',tasks}],base=constellationLayout(groups,440,800,420)
  const saved={...base,cards:base.cards.map(card=>card.id==='remote'?{...card,x:-1400,y:230}:card.id==='parent'?{...card,x:120,y:210}:{...card,x:120,y:200})}
  const view=restackCompanionOverview(saved,groups,desktop)
  const anchor=view.cards.find(card=>card.id==='parent'), child=view.cards.find(card=>card.id==='child')
  assert.ok(anchor.y+anchor.height*anchor.scale<=child.y || anchor.x+anchor.width*anchor.scale<=child.x || anchor.x>=child.x+child.width*child.scale || anchor.y>=child.y+child.height*child.scale)
  assert.deepEqual(view.projects,saved.projects)
  assert.deepEqual(view.cards.find(card=>card.id==='remote'),saved.cards.find(card=>card.id==='remote'))
  assert.equal(child.x,120);assert.equal(child.y,200)
  assert.deepEqual(companionLinkSource(child,tasks,view.cards),{kind:'cards',id:'parent'})
  assert.deepEqual(restackCompanionOverview(view,groups,desktop).cards.find(card=>card.id==='parent'),anchor,
    'The exposed endpoint must not drift on subsequent updates')
  const character={x:anchor.x,y:anchor.y,width:anchor.width*anchor.scale,height:anchor.height*anchor.scale}
  const avoiding=restackCompanionOverview(saved,groups,desktop,{},new Set(),character)
  const relocated=avoiding.cards.find(card=>card.id==='parent')
  const obstacles=[character,...avoiding.projects,...avoiding.cards.filter(card=>card.id!=='parent'&&!card.hidden)
    ].map(node=>({...node,width:node.width*(node.scale??1),height:node.height*(node.scale??1)}))
  for(const other of obstacles) assert.ok(relocated.x+relocated.width*relocated.scale<=other.x || relocated.x>=other.x+other.width
    || relocated.y+relocated.height*relocated.scale<=other.y || relocated.y>=other.y+other.height)
  assert.deepEqual(restackCompanionOverview(base,groups,desktop),restackCompanionOverview(base,groups,desktop,{},new Set(),character),
    'A non-overlapping parent must retain its position')
})
