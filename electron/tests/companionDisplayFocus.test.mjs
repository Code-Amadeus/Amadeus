import assert from 'node:assert/strict'
import test from 'node:test'
import { constellationLayout } from '../src/renderer/components/companionConstellationLayout.ts'
import { resolveCompanionDesktop } from '../src/main/windowPlacement.ts'
import { backDisplay, composeDisplayFocus, displayFocusLayout, nodeDisplay, selectDisplayTask } from '../src/renderer/components/companionDisplayFocus.ts'

const primary = { id: 1, bounds: { x: 0, y: 0, width: 2560, height: 1440 }, workArea: { x: 0, y: 0, width: 2560, height: 1392 } }
const secondary = { id: 2, bounds: { x: 2560, y: 0, width: 1080, height: 1920 }, workArea: { x: 2560, y: 0, width: 1080, height: 1872 } }
const desktop = resolveCompanionDesktop([primary, secondary], primary)
const group = (id, count) => ({ id, title: id, tasks: Array.from({length:count}, (_,i) => ({id:`${id}-${i}`,key:`${id}-${i}`,title:'Task',detail:'Result',phase:'ready'})) })
const groups = [group('one',2),group('two',2)]
const auto = constellationLayout(groups,1032,1820,750)
const overview = {...auto,cards:auto.cards.map(pose=>pose.id==='one-0'?{...pose,x:-1600,y:240}:pose)}
const focus = (projectId,taskId='') => ({trail:[{projectId,taskId}]})
const view = (displayId,selected) => displayFocusLayout(overview,groups,desktop,displayId,selected,'natural',112,displayId===1?1340:750)

test('reading a detached card does not relocate or miniaturize remote cards, even its own project', () => {
  const reading = view(1,focus('one','one-0')), result = composeDisplayFocus(overview,[reading])
  assert.equal(reading.projects.length,0,'Remote project badge must not be cloned onto the reading display')
  assert.deepEqual([...reading.taskIds],['one-0'])
  for(const pose of overview.projects) assert.deepEqual(result.projects.find(item=>item.id===pose.id),pose)
  for(const pose of overview.cards.filter(pose=>pose.id!=='one-0')) assert.deepEqual(result.cards.find(item=>item.id===pose.id),pose)
  const selected = result.cards.find(pose=>pose.id==='one-0')
  assert.equal(selected.focused,true)
  assert.equal(nodeDisplay(selected,desktop),1)
  assert.equal(result.cards.length,overview.cards.length)
})

test('two displays can navigate and return independently without changing persisted overview positions', () => {
  let selections = selectDisplayTask({},1,{projectId:'one',taskId:'one-0'})
  const primaryFocus = selections[1]
  selections = selectDisplayTask(selections,2,{projectId:'two',taskId:''})
  selections = selectDisplayTask(selections,2,{projectId:'two',taskId:'two-0'})
  assert.equal(selections[1],primaryFocus)
  const combined = composeDisplayFocus(overview,[view(1,selections[1]),view(2,selections[2])])
  assert.equal(combined.cards.filter(pose=>pose.focused).length,2)
  const primaryPose = combined.cards.find(pose=>pose.id==='one-0')
  selections = backDisplay(selections,2)
  assert.equal(selections[2].trail.length,1)
  selections = backDisplay(selections,2)
  assert.equal(selections[2],undefined)
  const restored = composeDisplayFocus(overview,[view(1,selections[1])])
  assert.deepEqual(restored.cards.find(pose=>pose.id==='one-0'),primaryPose)
  for(const pose of overview.cards.filter(pose=>nodeDisplay(pose,desktop)===2)) assert.deepEqual(restored.cards.find(item=>item.id===pose.id),pose)
  assert.deepEqual(backDisplay(selections,1),{})
  assert.deepEqual(composeDisplayFocus(overview,[]),overview)
})

test('local thumbnails contain only local tasks and preserve the actual project task count', () => {
  const reading = view(2,focus('two','two-0'))
  assert.equal(reading.cards.some(pose=>pose.id==='one-0'),false)
  assert.equal(reading.projects.find(pose=>pose.id==='one').count,2)
  assert.ok(reading.cards.filter(pose=>!pose.focused).every(pose=>pose.mini))
  assert.equal(new Set(reading.cards.map(pose=>pose.id)).size,reading.cards.length)
})

test('a side task on another display remains independent of its parent reading view', () => {
  const familyGroups=[{...groups[0],tasks:[groups[0].tasks[0],{...groups[0].tasks[1],parentTaskId:'one-0',sourceKind:'sidechat'}]}]
  const familyAuto=constellationLayout(familyGroups,1032,1820,750)
  const split={...familyAuto,cards:familyAuto.cards.map(pose=>pose.id==='one-1'?{...pose,x:-1600,y:240,depth:0,hidden:false}:pose)}
  const parentView=displayFocusLayout(split,familyGroups,desktop,2,focus('one','one-0'),'natural',112,750)
  const childView=displayFocusLayout(split,familyGroups,desktop,1,focus('one','one-1'),'natural',112,1340)
  assert.equal(parentView.cards.some(pose=>pose.id==='one-1'),false)
  assert.equal(childView.cards.some(pose=>pose.id==='one-0'),false)
  const result=composeDisplayFocus(split,[parentView,childView])
  assert.equal(result.cards.filter(pose=>pose.focused).length,2)
  assert.equal(new Set(result.cards.map(pose=>pose.id)).size,2)
})
