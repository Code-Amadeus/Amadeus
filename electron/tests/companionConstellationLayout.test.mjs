import assert from 'node:assert/strict'
import test from 'node:test'
import { constellationLayout } from '../src/renderer/components/companionConstellationLayout.ts'

const group = (id, count) => ({ id, title: id, tasks: Array.from({length:count}, (_,i) => ({
  id:id+'-'+i, phase:i===count-1?'attention':'ready', title:'Task '+i, detail:'Original result', key:id+'-'+i
})) })
const groups = Array.from({length:5},(_,i)=>group('project-'+i, i+2))
const layout = (items=groups,p='',t='',page=0) => constellationLayout(items,1032,1820,750,p,t,page)
const rect = p => ({left:p.x,top:p.y,right:p.x+p.width*p.scale,bottom:p.y+p.height*p.scale})
const overlap = (a,b) => a.left<b.right && b.left<a.right && a.top<b.bottom && b.top<a.bottom

test('one to five projects pack varied footprints with no collisions or character obstruction', () => {
  for(let seed=0;seed<40;seed++) {
    let previous=[]
    for(let n=1;n<=5;n++) {
      const items=Array.from({length:n},(_,i)=>group(`set-${seed}-${i}`,1+(seed+i)%5))
      const view=constellationLayout(items,1032,1820,750,'','',0,112,previous)
      assert.equal(view.compact,false)
      assert.equal(view.height,1820)
      const boxes=view.slots.map(s=>rect({...s,scale:1}))
      for(let i=0;i<boxes.length;i++) {
        assert.equal(overlap(boxes[i],{left:270,right:762,top:750,bottom:1820}),false)
        for(let j=i+1;j<boxes.length;j++) assert.equal(overlap(boxes[i],boxes[j]),false)
      }
      previous=view.slots
    }
  }
})

test('horizontal pair uses readable full size cards and slight vertical stagger', () => {
  const pair=layout([group('pair',2),group('single',1)]).cards.filter(c=>c.projectId==='pair')
  assert.ok(pair.every(c=>c.width===300&&!c.depth&&!c.hidden))
  assert.ok(Math.abs(pair[0].y-pair[1].y)<=32)
  assert.ok(Math.abs(pair[0].x-pair[1].x)>=320)
})

test('wording, status, order, navigation and removal preserve valid existing anchors', () => {
  const items=[group('one',1),group('pair',2),group('three',5)]
  const before=layout(items)
  const changed=items.toReversed().map(g=>({...g,tasks:g.tasks.map(t=>({...t,phase:'running',detail:'New progress'}))}))
  const updated=constellationLayout(changed,1032,1820,750,'pair','pair-0',0,92,before.slots)
  for(const s of updated.slots) assert.deepEqual(s,before.slots.find(p=>p.id===s.id))
  const removed=constellationLayout(changed.slice(1),1032,1820,750,'','',0,112,before.slots)
  for(const s of removed.slots) assert.deepEqual(s,before.slots.find(p=>p.id===s.id))
})

test('addition uses free space without moving existing anchors when it fits', () => {
  const before=layout([group('one',1)])
  const after=constellationLayout([group('one',1),group('two',1)],1032,1820,750,'','',0,112,before.slots)
  assert.equal(after.slots[0].x,before.slots[0].x)
  assert.equal(after.slots[0].y,before.slots[0].y)
})

test('different project sets have spatial preferences beyond fixed corner slots', () => {
  const anchors=new Set(Array.from({length:12},(_,i)=>{
    const s=layout([group('identity-'+i,1)]).slots[0]
    return `${Math.round(s.x)},${Math.round(s.y)}`
  }))
  assert.ok(anchors.size>8)
})

test('five projects remain visible without scrolling, with clear character centre and no overlapping fronts', () => {
  const view=layout()
  assert.equal(view.projects.length,5)
  assert.equal(view.height,1820)
  const fronts=view.cards.filter(c=>!c.hidden&&!c.depth)
  assert.equal(new Set(fronts.map(c=>c.projectId)).size,5)
  const centre={left:270,right:762,top:750,bottom:1820}
  const nodes=[...fronts,...view.projects].map(rect)
  for(const r of nodes) {
    assert.ok(r.left>=0 && r.right<=1032 && r.top>=0 && r.bottom<=1820)
    assert.equal(overlap(r,centre),false)
  }
  for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++) assert.equal(overlap(nodes[i],nodes[j]),false)
  for(const project of view.projects) {
    if (project.stacked) assert.equal(project.frontId,groups.find(g=>g.id===project.id).tasks.at(-1).id)
    assert.equal(view.cards.filter(c=>c.projectId===project.id&&!c.hidden).length,project.stacked ? Math.min(3,project.count) : project.count)
  }
})

test('a small active project keeps both tasks visible when its upper slot has enough room', () => {
  const view=layout([group('first',1),group('pair',2),group('third',1)])
  assert.equal(view.projects.find(p=>p.id==='pair').stacked,false)
  assert.equal(view.cards.filter(c=>c.projectId==='pair'&&!c.hidden&&!c.depth).length,2)
  assert.equal(view.height,1820)
})

test('each real task has one pose and one project identity during task focus; selected task has no rail duplicate', () => {
  const before=layout(), after=layout(groups,'project-1','project-1-0')
  assert.equal(after.cards.length,new Set(after.cards.map(c=>c.id)).size)
  assert.equal(after.cards.filter(c=>c.focused).length,1)
  const selected=after.cards.find(c=>c.focused)
  assert.equal(selected.projectId,'project-1')
  assert.equal(selected.mini,false)
  for(const item of after.cards.filter(c=>!c.focused&&!c.hidden)) {
    assert.equal(item.mini,true)
    assert.ok(item.scale<1)
    assert.ok(item.x>selected.x+selected.width)
    assert.notDeepEqual(rect(item),rect(before.cards.find(c=>c.id===item.id)))
  }
  // The two-task project has exactly two task connections, never a copy of the focused card.
  const pair=[group('pair',2)]
  assert.equal(layout(pair,'pair','pair-0').cards.filter(c=>!c.hidden&&!c.depth).length,2)
})

test('project expansion preserves all tasks across bounded pages and returning restores overview positions', () => {
  const many=[group('many',11),group('other',2)]
  const before=layout(many)
  const first=layout(many,'many')
  assert.equal(first.projects.find(p=>p.id==='many').mini,false)
  assert.ok(first.taskPages>1)
  const seen=new Set()
  for(let page=0;page<first.taskPages;page++) {
    const expanded=layout(many,'many','',page)
    for(const card of expanded.cards.filter(c=>c.projectId==='many'&&!c.hidden)) {
      assert.equal(card.scale,1)
      assert.ok(rect(card).bottom<750)
      seen.add(card.id)
    }
  }
  assert.equal(seen.size,11)
  assert.deepEqual(layout(many),before)
  assert.equal(layout(many,'many','',99).taskPage,first.taskPages-1)
})

test('many tasks do not overflow the five-project rail; badges retain total task counts', () => {
  const busy=Array.from({length:5},(_,i)=>group('busy-'+i,30))
  const view=layout(busy,'busy-0','busy-0-0')
  for(const c of view.cards.filter(c=>!c.hidden)) assert.ok(rect(c).bottom<1820)
  assert.ok(view.projects.every(p=>p.count===30))
  assert.equal(view.height,1820)
})

test('child agents connect to existing parents and do not inflate project root counts', () => {
  const parent=group('parent',7)
  parent.tasks.push({id:'child',parentTaskId:'parent-6',phase:'running'})
  const view=layout([parent],'parent','child')
  assert.equal(view.projects[0].count,7)
  assert.equal(view.cards.filter(c=>c.id==='child').length,1)
  assert.equal(view.cards.find(c=>c.id==='parent-6').hidden,false)
  assert.ok(view.cards.find(c=>c.id==='child').focused)
})

test('small display fallback preserves access with readable cards and bounded width', () => {
  const view=constellationLayout(groups,408,760,350)
  assert.ok(view.compact)
  assert.ok(view.height>760)
  assert.equal(view.projects.length,5)
  for(const c of view.cards.filter(c=>!c.hidden)) {
    assert.ok(c.width>=240)
    assert.ok(rect(c).right<=408)
  }
})
