// Opt-in fixture integration check. Uses Chromium pointer input against the
// production renderer on the actual desktop; it is not an OS mouse acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

module.exports = async function verifyCrossDisplay({win, desktop, origin, output}) {
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const js = source => win.webContents.executeJavaScript(source);
  const click = async (selector, button = 'left') => {
    const point = await js(`(() => {const b=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2)}})()`);
    // Deliberately do not activate the floating palette.
    win.webContents.sendInputEvent({type:'mouseMove',...point});
    win.webContents.sendInputEvent({type:'mouseDown',...point,button,clickCount:1});
    win.webContents.sendInputEvent({type:'mouseUp',...point,button,clickCount:1});
    await pause(850);
  };
  const snapshot = () => js(`(() => {
    const rect = element => element.getBoundingClientRect().toJSON();
    return {
      viewport:{width:innerWidth,height:innerHeight},
      preferences:JSON.parse(localStorage.getItem('amadeus.companion.layout.v1')),
      editing:document.querySelector('.floating-companion').classList.contains('is-layout-editing'),
      nodes:[...document.querySelectorAll('[data-task-id],[data-project-id]')].filter(e=>getComputedStyle(e).visibility==='visible')
        .map(e=>({id:e.dataset.taskId || e.dataset.projectId, ...rect(e)})),
      character:rect(document.querySelector('.floating-companion-render')),
      reading:document.querySelector('.companion-focus-panel') && rect(document.querySelector('.companion-focus-panel')),
      readers:[...document.querySelectorAll('.companion-display-reading')].map(e=>({displayId:Number(e.dataset.displayId),...rect(e)})),
      dismiss:[...document.querySelectorAll('.companion-focus-dismiss')].map(e=>({displayId:Number(e.dataset.displayId),color:getComputedStyle(e).backgroundColor,...rect(e)})),
      edges:[...document.querySelectorAll('[data-edge-task]')].map(e=>({id:e.dataset.edgeTask,path:e.querySelector('path').getAttribute('d')}))
    };
  })()`);
  const capture = async name => fs.writeFileSync(path.join(output,`cross-display-${name}.png`),(await win.webContents.capturePage()).toPNG());
  const drag = async (selector, to) => {
    const from = await js(`(() => {const b=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2)}})()`);
    // Deliberately do not activate the floating palette.
    win.webContents.sendInputEvent({type:'mouseMove',...from});
    win.webContents.sendInputEvent({type:'mouseDown',...from,button:'left',clickCount:1});
    await pause(50);
    for(let step=1;step<=20;step++) {
      win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(from.x+(to.x-from.x)*step/20),y:Math.round(from.y+(to.y-from.y)*step/20),modifiers:['leftButtonDown']});
      await pause(20);
    }
    win.webContents.sendInputEvent({type:'mouseUp',...to,button:'left',clickCount:1});
    await pause(1000);
  };
  const near = (a,b,label) => {
    for(const key of ['x','y','width','height']) assert.ok(Math.abs(a[key]-b[key])<.5, `${label}: ${key} changed ${a[key]} -> ${b[key]}`);
  };
  const inside = (box, area) => box.x>=area.x-1 && box.y>=area.y-1 && box.right<=area.x+area.width+1 && box.bottom<=area.y+area.height+1;
  const target = desktop.displays.find(display=>display.workArea.x!==desktop.home.x || display.workArea.y!==desktop.home.y);
  assert.ok(target,'Two displays are required');
  const targetArea = {...target.workArea,x:target.workArea.x-desktop.bounds.x,y:target.workArea.y-desktop.bounds.y};
  await pause(2000);
  // This preview has its own isolated userData and fixture origin.
  // Unmount first: the renderer persists its current layout on pagehide.
  // Clearing storage while it is mounted would be overwritten during reload.
  await win.loadURL('about:blank');
  await win.webContents.session.clearStorageData({origin,storages:['localstorage']});
  await win.loadURL(origin+'/ui/index.html?companionWindow=1');
  await pause(1500);
  await fetch(origin+'/families/2',{method:'POST'});
  await pause(1200);
  await click('.floating-companion-character-hit');
  await click('.floating-companion-toolbar button:nth-of-type(4)');
  await click('[data-layout-edit="cards"]');
  const before = await snapshot();
  assert.equal(await js("document.querySelectorAll('.companion-layout-controls input[type=checkbox]').length"),0,'Layout editing is an explicit mode, not a checkbox');
  await click('[data-task-id="organic-0-0"] .companion-thought-open');
  await click('[data-task-id="organic-0-0"] .companion-open-source');
  const editClicked=await snapshot();
  assert.equal(editClicked.editing,true,'An ordinary card click must not leave editing');
  assert.equal(editClicked.readers.length,0,'An ordinary card click must not open reading during editing');
  for(const node of before.nodes) near(node,editClicked.nodes.find(item=>item.id===node.id),'Editing clicks changed placement');
  assert.deepEqual(before.viewport,{width:desktop.bounds.width,height:desktop.bounds.height});
  assert.deepEqual(win.getBounds(),desktop.bounds);
  await capture('before');
  const residentId='organic-0-0';
  await drag(`[data-task-id="${residentId}"]`, {x:Math.round(targetArea.x+targetArea.width*.76),y:Math.round(targetArea.y+targetArea.height*.26)});
  const cardMoved = await snapshot(), resident = cardMoved.nodes.find(node=>node.id===residentId);
  assert.ok(inside(resident,targetArea),'The individual card must land in the target display');
  near(before.character,cardMoved.character,'Individual card moved the character');
  for(const node of before.nodes.filter(node=>node.id!==residentId)) near(node,cardMoved.nodes.find(item=>item.id===node.id),'Individual card moved another node');
  await capture('card-moved');
  // Reproduce the reported journey BEFORE moving the character: only one card
  // has crossed. Reading must leave every source-display node in overview.
  await click('[data-layout-finish]');
  await click('.companion-layout-controls header button');
  await click(`[data-task-id="${residentId}"] .companion-thought-open`);
  const splitReading = await snapshot();
  for(const node of cardMoved.nodes.filter(node=>node.id!==residentId)) near(node,splitReading.nodes.find(item=>item.id===node.id),'Opening a detached card moved a remote node');
  near(cardMoved.character,splitReading.character,'Reading moved the character');
  assert.equal(splitReading.readers.length,1);
  assert.equal(splitReading.dismiss.length,1);
  assert.ok(inside(splitReading.dismiss[0],targetArea),'Blank return surface must stay on its own display');
  assert.notEqual(splitReading.dismiss[0].color,'rgba(0, 0, 0, 0)','Windows needs nonzero alpha for blank pixel hit testing');
  await capture('split-reading');
  await click('[data-task-id="organic-1-0"] .companion-thought-open');
  const bothReading=await snapshot();
  assert.equal(bothReading.readers.length,2,'Both displays must have independent readers');
  near(splitReading.nodes.find(node=>node.id===residentId),bothReading.nodes.find(node=>node.id===residentId),'Remote interaction disturbed the first reader');
  await capture('both-reading');
  await click(`.companion-focus-dismiss[data-display-id="${target.id}"]`);
  const primaryClosed=await snapshot();
  assert.equal(primaryClosed.readers.length,1);
  assert.notEqual(primaryClosed.readers[0].displayId,target.id);
  near(cardMoved.nodes.find(node=>node.id===residentId),primaryClosed.nodes.find(node=>node.id===residentId),'Blank click did not restore detached card');
  for(const node of bothReading.nodes.filter(node=>node.id!==residentId)) near(node,primaryClosed.nodes.find(item=>item.id===node.id),'Returning one display affected the other');
  await click('.companion-focus-dismiss');
  const splitReturned=await snapshot();
  for(const node of cardMoved.nodes) near(node,splitReturned.nodes.find(item=>item.id===node.id),'Returning both screens changed overview placement');
  assert.equal(splitReturned.readers.length,0);
  fs.writeFileSync(path.join(output,'focus-isolation-verification.json'),JSON.stringify({verified:true,at:new Date().toISOString(),desktop,cardMoved,splitReading,bothReading,primaryClosed,splitReturned},null,2));
  await click('.floating-companion-toolbar button:nth-of-type(4)');
  await click('[data-layout-edit="scene"]');
  await drag('.floating-companion-character-hit',{x:Math.round(targetArea.x+targetArea.width*.45),y:Math.round(targetArea.y+targetArea.height*.75)});
  const sceneMoved = await snapshot();
  near(resident,sceneMoved.nodes.find(node=>node.id===residentId),'Resident card followed the character');
  assert.ok(inside(sceneMoved.character,targetArea),'Character must fit the target work area');
  for(const node of sceneMoved.nodes) assert.ok(inside(node,targetArea),`Carried node ${node.id} must fit the target work area`);
  assert.equal(new Set(sceneMoved.nodes.map(node=>node.id)).size,before.nodes.length,'Task identity duplicated');
  await capture('scene-moved');
  await click('[data-layout-finish]');
  await click('.companion-layout-controls header button');
  await click(`[data-task-id="${residentId}"] .companion-thought-open`);
  const reading = await snapshot();
  assert.ok(reading.reading && inside(reading.reading,targetArea),'Reading must open on the card display');
  assert.ok(reading.reading.width>=500,'Reading must not inherit thumbnail scaling');
  await capture('reading');
  await click('.companion-focus-dismiss');
  const returned = await snapshot();
  for(const node of sceneMoved.nodes) near(node,returned.nodes.find(item=>item.id===node.id),'Reading changed the saved overview');
  await win.loadURL(origin+'/ui/index.html?companionWindow=1');
  await pause(2000);
  const reopened = await snapshot();
  assert.equal(reopened.editing,false,'Edit mode must lock on reopening');
  for(const node of sceneMoved.nodes) near(node,reopened.nodes.find(item=>item.id===node.id),'Reopening lost a placement');
  near(sceneMoved.character,reopened.character,'Reopening lost the character');
  await capture('reopened');
  fs.writeFileSync(path.join(output,'cross-display-verification.json'),JSON.stringify({verified:true,kind:'Chromium pointer integration on actual displays; not OS mouse acceptance',at:new Date().toISOString(),desktop,nativeBounds:win.getBounds(),before,cardMoved,sceneMoved,reading,returned,reopened},null,2));
  // Leave the exact split-screen regression available for native blank-click
  // acceptance; fixture preferences are isolated from the daily application.
  await click('.floating-companion-character-hit');
  await click('.floating-companion-toolbar button:nth-of-type(4)');
  await click('[data-layout-edit="scene"]');
  await click('.companion-layout-actions button:nth-child(3)');
  await click('.companion-layout-actions:last-child button:first-child');
  await click('[data-layout-finish]');
  await click('[data-layout-edit="cards"]');
  await drag(`[data-task-id="${residentId}"]`,{x:Math.round(targetArea.x+targetArea.width*.76),y:Math.round(targetArea.y+targetArea.height*.26)});
  await click('[data-layout-finish]');
  await click('.companion-layout-controls header button');
  await click('.floating-companion-character-hit');
  await click(`[data-task-id="${residentId}"] .companion-thought-open`);

  // Context anchors and explicit regrouping share the same production drag path.
  await win.loadURL('about:blank');
  await win.webContents.session.clearStorageData({origin,storages:['localstorage']});
  await fetch(origin+'/context-fixture',{method:'POST'});
  await win.loadURL(origin+'/ui/index.html?companionWindow=1');
  await pause(2200);
  await click('.floating-companion-character-hit');
  await click('.floating-companion-toolbar button:nth-of-type(4)');
  await click('[data-layout-edit="cards"]');
  const hierarchyBefore=await snapshot(), childId='stack-0-0', peerProject='project:stack-3';
  const childDestination={x:Math.round(targetArea.x+targetArea.width*.6),y:Math.round(targetArea.y+targetArea.height*.3)};
  await drag(`[data-task-id="${childId}"]`,childDestination);
  const childMoved=await snapshot();
  for(const node of hierarchyBefore.nodes.filter(node=>node.id!==childId)) near(node,childMoved.nodes.find(item=>item.id===node.id),'Detached side card moved a remote node');
  await drag(`[data-project-id="${peerProject}"]`,{x:Math.round(targetArea.x+targetArea.width*.75),y:Math.round(targetArea.y+targetArea.height*.12)});
  const peerMoved=await snapshot();
  near(childMoved.nodes.find(node=>node.id===childId),peerMoved.nodes.find(node=>node.id===childId),'Other project drag moved detached side card');
  await drag('[data-project-id="project:stack-0"]',{x:desktop.home.x-desktop.bounds.x+410,y:330});
  const parentMoved=await snapshot();
  near(peerMoved.nodes.find(node=>node.id===childId),parentMoved.nodes.find(node=>node.id===childId),'Parent project drag pulled a remote side card back');
  await click('[data-project-id="project:stack-0"]','right');
  await click('.companion-project-menu button');
  const gathered=await snapshot();
  const homeArea={...desktop.home,x:desktop.home.x-desktop.bounds.x,y:desktop.home.y-desktop.bounds.y};
  for(const id of ['project:stack-0','inactive-parent','stack-0-0','stack-0-1']) assert.ok(inside(gathered.nodes.find(node=>node.id===id),homeArea),'Explicit gather must collect its whole project');
  for(const node of parentMoved.nodes.filter(node=>node.id===peerProject||node.id.startsWith('stack-3-'))) near(node,gathered.nodes.find(item=>item.id===node.id),'Gather moved another project');
  await drag(`[data-task-id="${childId}"]`,childDestination);
  await click('[data-layout-finish]');
  await click('.companion-layout-controls header button');
  const hierarchySplit=await snapshot();
  await click(`[data-task-id="${childId}"] .companion-thought-open`);
  const hierarchyReading=await snapshot();
  for(const node of hierarchySplit.nodes.filter(node=>inside(node,homeArea))) near(node,hierarchyReading.nodes.find(item=>item.id===node.id),'Reading side card changed a source-display node');
  assert.ok(hierarchyReading.nodes.find(node=>node.id===peerProject).width<hierarchySplit.nodes.find(node=>node.id===peerProject).width,'Only the other local project should become miniature');
  assert.equal((hierarchyReading.edges.find(edge=>edge.id===childId).path.match(/M /g)||[]).length,2,'A cross-display parent edge must have two continuations');
  await capture('hierarchy-reading');
  await click(`[data-task-id="${childId}"]`,'right');
  assert.ok(await js("document.querySelector('.companion-project-menu').textContent.includes('收起详情')"));
  await click('.companion-project-menu button');
  assert.equal((await snapshot()).readers.length,0,'Context menu must restore the detached card without changing remote layout');
  await click(`[data-task-id="${childId}"] .companion-thought-open`);
  for(const display of desktop.displays) {
    const area={x:display.workArea.x-desktop.bounds.x,y:display.workArea.y-desktop.bounds.y,width:display.workArea.width,height:display.workArea.height};
    fs.writeFileSync(path.join(output,`hierarchy-reading-display-${display.id}.png`),(await win.webContents.capturePage(area)).toPNG());
  }
  fs.writeFileSync(path.join(output,'hierarchy-drag-verification.json'),JSON.stringify({verified:true,kind:'Chromium pointer integration; OS drag remains a separate acceptance',at:new Date().toISOString(),hierarchyBefore,childMoved,peerMoved,parentMoved,gathered,hierarchySplit,hierarchyReading},null,2));
  await win.loadURL('about:blank');
  await win.webContents.session.clearStorageData({origin,storages:['localstorage']});
  await fetch(origin+'/active-parent-fixture',{method:'POST'});
  await win.loadURL(origin+'/ui/index.html?companionWindow=1');
  await pause(2300);
  const activeOverview=await snapshot();
  const parents=await js("[...document.querySelectorAll('[data-task-id]')].filter(e=>e.dataset.taskId.endsWith('-0')).map(e=>({id:e.dataset.taskId,visible:getComputedStyle(e).visibility,classes:e.className}))");
  assert.ok(parents.every(parent=>parent.visible==='visible'&&!parent.classes.includes('is-stack-back')),'Every active parent must remain visible above its side pile');
  await click('[data-project-id="project:stack-0"]');
  const spread=await snapshot();
  assert.equal(spread.readers.length,1);
  await click('[data-project-id="project:stack-0"]','right');
  assert.ok(await js("document.querySelector('.companion-project-menu').textContent.includes('收起卡组')"));
  await click('.companion-project-menu button');
  const folded=await snapshot();
  assert.equal(folded.readers.length,0);
  for(const node of activeOverview.nodes) near(node,folded.nodes.find(item=>item.id===node.id),'Collapsing must restore the saved overview');
  for(const display of desktop.displays) {
    const area={x:display.workArea.x-desktop.bounds.x,y:display.workArea.y-desktop.bounds.y,width:display.workArea.width,height:display.workArea.height};
    fs.writeFileSync(path.join(output,`active-parent-display-${display.id}.png`),(await win.webContents.capturePage(area)).toPNG());
  }
  fs.writeFileSync(path.join(output,'interaction-modes-verification.json'),JSON.stringify({verified:true,kind:'Chromium pointer integration; no OS focus activation',at:new Date().toISOString(),editClicked,activeOverview,parents,folded},null,2));
};
