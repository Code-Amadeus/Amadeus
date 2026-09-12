// Chromium input against the real compiled renderer; no Codex or speech calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
module.exports = async function verify({win,desktop,origin,output}) {
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const js=source=>win.webContents.executeJavaScript(source);
  const click=async selector=>{
    const p=await js(`(()=>{const b=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2)}})()`);
    win.webContents.sendInputEvent({type:'mouseMove',...p});
    win.webContents.sendInputEvent({type:'mouseDown',...p,button:'left',clickCount:1});
    win.webContents.sendInputEvent({type:'mouseUp',...p,button:'left',clickCount:1});
    await pause(850);
  };
  const snapshot=()=>js(`(()=>{
    const rect=e=>e.getBoundingClientRect().toJSON();
    return {cards:[...document.querySelectorAll('[data-task-id]')].map(e=>({id:e.dataset.taskId,hidden:getComputedStyle(e).visibility!=='visible',mini:e.classList.contains('is-miniature'),...rect(e),
      body:e.querySelector('.companion-thought-excerpt')?.textContent,bodyHeight:e.querySelector('.companion-thought-excerpt')?.clientHeight,
      font:e.querySelector('.companion-markdown')&&getComputedStyle(e.querySelector('.companion-markdown')).fontSize})),
      projects:[...document.querySelectorAll('[data-project-id]')].map(e=>({id:e.dataset.projectId,...rect(e)})),
      edges:[...document.querySelectorAll('[data-edge-task]')].map(e=>({id:e.dataset.edgeTask,path:e.querySelector('path').getAttribute('d')})),
      pagination:!!document.querySelector('.companion-project-task-pages'),
      reading:!!document.querySelector('.companion-focus-panel'),
      profile:JSON.parse(localStorage.getItem('amadeus.companion.layout.v1'))}
  })()`);
  const area={x:desktop.home.x-desktop.bounds.x,y:desktop.home.y-desktop.bounds.y,width:desktop.home.width,height:desktop.home.height};
  const capture=async name=>fs.writeFileSync(path.join(output,`project-map-${name}.png`),(await win.webContents.capturePage(area)).toPNG());
  await win.loadURL('about:blank');
  await win.webContents.session.clearStorageData({origin,storages:['localstorage']});
  await win.loadURL(origin+'/ui/index.html?companionWindow=1');
  await pause(2200);
  const evidence=[];
  for(const count of [9,15,25]) {
    await fetch(origin+`/project-map/${count}`,{method:'POST'}); await pause(1400);
    const before=await snapshot();
    await click('[data-project-id="project:map"]');
    const expanded=await snapshot(),cards=expanded.cards.filter(c=>/^map-\d+$/.test(c.id));
    assert.equal(cards.length,count);assert.ok(cards.every(c=>!c.hidden&&!c.mini));
    assert.equal(expanded.pagination,false);
    for(const c of cards) {
      assert.ok(c.x>=area.x&&c.y>=area.y&&c.right<=area.x+area.width+1&&c.bottom<=area.y+area.height+1);
      assert.ok(c.body.includes('全文末尾标记'),'Original body was replaced by a summary');
      assert.equal(c.font,'14px');
    }
    if(count===9) assert.ok(cards.every(c=>c.width>=220&&c.bodyHeight>=42),'Nine tasks lost readable preview space');
    assert.ok(expanded.cards.filter(c=>c.mini).length===10,'Other project identities disappeared from the rail');
    assert.ok(expanded.edges.filter(e=>/^map-\d+$/.test(e.id)).every(e=>e.path));
    await capture(`${count}-expanded`);
    await click('[data-task-id="map-5"] .companion-thought-open');
    assert.ok((await snapshot()).reading);
    assert.ok(await js("document.querySelector('[data-reading-id=\"current\"]').textContent.includes('全文末尾标记')"));
    assert.ok(await js("document.querySelector('[data-reading-id=\"current\"] table')!==null"),'Original Markdown table not rendered');
    await capture(`${count}-reading`);
    await click('.companion-return-overview');
    const returned=await snapshot();
    assert.equal(returned.reading,false);assert.deepEqual(returned.cards,expanded.cards);
    await click('.companion-return-overview');
    const overview=await snapshot();
    assert.deepEqual(overview.cards,before.cards,'Return lost original overview placements');
    evidence.push({count,before,expanded,returned});
  }
  fs.writeFileSync(path.join(output,'project-map-verification.json'),JSON.stringify({verified:true,at:new Date().toISOString(),desktop,evidence},null,2));
};
