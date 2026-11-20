// Full-pack content checks run after measurement, never in resource rankings.
export async function runBundleContentChecks({host = window, timeoutMs = 10000} = {}) {
  const s=host.renderApp._sprite, experiment=host.__frameVideoExperiment;
  const renderer=host.wallpaperApp.scene.app.renderer, checks=[], captures=[];
  const result={checks,captures,clips:[],error:null};
  const check=(passed,name,details={})=>{checks.push({name,passed:!!passed,...details});if(!passed)throw Error(name);};
  const until=async(name,fn)=>{const end=performance.now()+timeoutMs;while(!fn()){
    if(performance.now()>end)throw Error('Timed out: '+name);await new Promise(r=>setTimeout(r,20));}};
  function capture(texture,name,label,index) {
    const sprite=new host.PIXI.Sprite(texture);
    try {
      const values=renderer.extract.pixels(sprite);
      let alpha=0,rgb=0;for(let i=0;i<values.length;i++) {if(i%4===3)alpha+=values[i];else rgb+=values[i];}
      check(alpha>0&&rgb>0, name+' has nonblank pixels', {label,index,width:texture.width,height:texture.height});
      captures.push({name,label,sourceIndex:index,png:renderer.extract.canvas(sprite).toDataURL('image/png')});
      return {width:texture.width,height:texture.height,alpha,rgb};
    } finally {sprite.destroy({texture:false,baseTexture:false});}
  }
  try {
    host.renderApp.releaseCharacter();s.setIdleAnimation(false);
    for (const label of experiment.labels) {
      s.clearHold();s.setSpeaking(false);s.setEmotion(label);
      const config=s._experimentalFrameVideos.get(label), first=config.descriptor.headFrameCount;
      const cfg=s._mouthConfigs[label];
      if(cfg) {s.setSpeaking(true);s.setMouth(0);}
      check(!config.failed,'video source available: '+label);
      const source=s._frameVideoSource;
      source.requestWindow([{index:first,priority:100,deadline:Infinity}]);
      await until(label+' first body',()=>source.has(first));
      const video=source.texture(first);source.retainDisplay(video);
      let videoPixels;
      try {videoPixels=capture(video,`bundle-${label}-video-${first}`,label,first);}
      finally {source.releaseDisplay(video);}
      let videoMouth=null;
      if(cfg) {
        const selected=s._sampleFrameIndex(label,first+2);
        source.requestWindow([{index:selected,priority:100,deadline:Infinity}]);
        await until(label+' sampled body',()=>source.has(selected));
        s._showFrame(selected);
        await until(label+' video mouth layers',()=>s._videoDisplay?.source===source&&s._mouthTextures[label].every(Boolean));
        s._updateMouthLayer();
        const anchor=s._getMouthAnchor(cfg),expected=cfg.anchorTrack?.[s._activeFrameIdx]||cfg;
        check(['cx','cy','width','height'].every(k=>anchor[k]===(Number.isFinite(expected[k])?expected[k]:cfg[k])),
          'mouth anchor follows video source: '+label,{sourceIndex:s._activeFrameIdx});
        check(cfg.mode!=='silence_close'||s._mouthOverlay.visible,'silence closure overlays video: '+label);
        videoMouth={sourceIndex:s._activeFrameIdx,anchor,overlayVisible:s._mouthOverlay.visible};
      }
      s.holdFrame(first);
      await until(label+' exact static hold',()=>s._activeFrameIdx===first&&!s._videoDisplay&&s.sprite.texture===s._frames[label][first]);
      const staticPixels=capture(s.sprite.texture,`bundle-${label}-static-${first}`,label,first);
      check(videoPixels.width===staticPixels.width&&videoPixels.height===staticPixels.height,'video/static canvas matches: '+label);
      let mouth=null;
      if(cfg) {
        s.setSpeaking(false);s.setMouth(0);s.holdClosedFrame();
        await until(label+' closed static hold',()=>s._held&&s._activeFrameIdx===s._heldFrameIdx&&!s._videoDisplay);
        const index=s._activeFrameIdx, anchor=s._getMouthAnchor(cfg), expected=cfg.anchorTrack?.[index]||cfg;
        check(['cx','cy','width','height'].every(k=>anchor[k]===(Number.isFinite(expected[k])?expected[k]:cfg[k])),
          'mouth anchor follows displayed source: '+label,{sourceIndex:index});
        await until(label+' closed mouth overlay',()=>s._mouthTextures[label].every(Boolean));
        s._updateMouthLayer();
        check(cfg.mode!=='silence_close'||s._mouthOverlay.visible,'silence closure remains visible: '+label);
        mouth={sourceIndex:index,anchor,mode:cfg.mode,overlayVisible:s._mouthOverlay.visible};
      }
      result.clips.push({label,firstBody:first,videoPixels,staticPixels,mouth,videoMouth});
    }
  } catch(error) {result.error=String(error?.message||error);checks.push({name:'all material content checks complete',passed:false,error:result.error});}
  result.passed=!result.error&&result.clips.length===experiment.labels.length;
  return result;
}
