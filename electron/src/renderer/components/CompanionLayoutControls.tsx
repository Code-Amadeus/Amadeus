import type { CompanionLayoutController } from './useCompanionLayout'
import type { SceneTransform } from './companionLayoutPreferences'

export default function CompanionLayoutControls({ layout, fit, changeScene }: { layout: CompanionLayoutController; fit: () => void; changeScene: (scene: SceneTransform) => void }) {
  const { mode, edit, profile, selectMode, setEdit } = layout
  return <section className="companion-layout-controls" data-companion-hit aria-label="布局设置">
    <header><strong>{edit === 'cards' ? '正在编辑卡片位置' : edit === 'scene' ? '正在调整整体构图' : '布局'}</strong><button aria-label="关闭布局设置" onClick={() => { layout.setPanelOpen(false); setEdit('locked') }}>×</button></header>
    <fieldset disabled={!layout.display}>
      {edit === 'locked' && <>
      <legend>排列方式</legend>
      <div className="companion-layout-modes">
        {([['ordered', '规整排列'], ['natural', '自然错落'], ['scattered', '随机散布']] as const).map(([value, title]) =>
          <button key={value} aria-pressed={mode === value} onClick={() => selectMode(value)}>{title}</button>)}
      </div>
      <div className="companion-layout-entry-actions">
        <button data-layout-edit="cards" onClick={() => setEdit('cards')}>编辑卡片位置 <span>↗</span></button>
        <button data-layout-edit="scene" onClick={() => setEdit('scene')}>调整整体位置与大小 <span>↗</span></button>
      </div>
      <p>Ctrl + Alt + A 可唤到前面，操作卡片不会抢走当前应用的输入焦点。</p>
      </>}
      {edit === 'cards' && <p>拖动整张卡片或项目名即可移动。编辑时点击不会打开详情；退出时自动保存。叠放的卡组一起移动，主对话带走同屏分支。</p>}
      {edit === 'scene' && <>
        <p>拖人物可跨屏，只带走出发屏幕上的项目和卡片。其他屏幕的卡片保持原位。</p>
        <label className="companion-layout-zoom"><span>整体大小</span><output>{Math.round(profile.scene.scale * 100)}%</output>
          <input aria-label="整体缩放" type="range" min="60" max="120" step="1" value={Math.round(profile.scene.scale * 100)}
            onChange={event => changeScene({ ...profile.scene, scale: Number(event.target.value) / 100 })} /></label>
        <div className="companion-layout-actions"><button onClick={() => changeScene({ ...profile.scene, scale: 1 })}>恢复 100%</button>
          <button onClick={fit}>适合屏幕</button><button onClick={() => changeScene({ x: 0, y: 0, scale: 1 })}>恢复整体位置</button></div>
      </>}
      <div className="companion-layout-actions">
        {mode === 'scattered' && <button onClick={layout.shuffle}>换一组排布</button>}
        <button onClick={layout.resetLayout}>恢复本模式自动排布</button>
        {edit !== 'locked' && <button data-layout-finish onClick={() => setEdit('locked')}>完成调整</button>}
      </div>
    </fieldset>
  </section>
}
