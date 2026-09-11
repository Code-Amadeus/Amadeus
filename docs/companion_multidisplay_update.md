# Companion multi-display and card interaction milestone

Date: 2026-09-11. Incremental update to `codex/companion-prototype`, based on the published `f3b677d` snapshot. Related interaction work was developed and exercised locally through `f1e5736`; only the Companion presentation changes are transferred here.

## What changed

- Cards can move independently across displays. Moving a project or parent carries its related nodes on the departure display; cards already on another display remain independent. Explicit project regrouping is available from its editing menu.
- Opening or closing a reader rearranges only that display. Two displays can hold independent readers. The selected card is represented once rather than duplicated in the thumbnail rail.
- Layout offers ordered, scattered and organic placement. Organic placement reserves whole family footprints, prefers existing anchors and avoids a fixed five-project template. Each mode retains its own layout; restore returns to automatic placement.
- Card editing and whole-composition adjustment are explicit actions. Whole-card dragging works in edit mode; ordinary card actions are suspended until editing finishes. Finishing saves automatically. Moving/scaling the character carries the composition on its departure display.
- Only sibling leaf tasks form a pile. Compact parents remain separate connection endpoints. Up to seven frosted edges communicate stack depth; larger piles remain accessible through selection and spread pages. Manual front-card choice and attention priority share one ordering rule.
- An overlapping compact parent from an older saved pile is moved to the nearest free gap on its own display, preserving other placements. If no free gap exists, it does not force other nodes aside.
- Right-click menus offer collapse, return, editing and explicit regrouping. Project-to-root lines route around intervening cards. Moving connection endpoints are measured once per frame before paths are updated.
- On Windows, the Companion is a non-activating palette behind the current application. Ordinary pointer interaction does not request application focus. Ctrl+Alt+A raises it without focusing it. Explicit source-navigation actions may still switch applications.

## Source and integration boundaries

The virtual desktop geometry is owned by Electron and exposed only to its trusted Companion renderer. Saved overview positions determine display membership; temporary reader/thumbnail positions never acquire authority over another display's layout. Parent identities remain independent of where a card is placed.

This update keeps the public branch's upstream Wallpaper implementation, single-screen bounds safeguards, source-navigation allowlist, opt-in observer and muted-by-default reminder setting. It does not transfer the daily branch's Codex startup watcher, audio pipeline changes, experimental temporary-side-chat adapter or private settings/assets. The graph can render identified child tasks and supplied ancestor contexts; that does not establish coverage of every external Codex side-chat format.

No new animation dependency or purchased material is introduced. The existing skin hooks remain; this milestone does not settle the long-term design language.

## Validation

Validation for this public-branch integration is recorded in [Companion validation](companion_validation.md). The fixture harness uses the compiled production components and its own profile; its default mode makes no Codex or model request.

The cross-display regression can be repeated on a Windows machine with two displays and the optional character bundle installed:

```powershell
Push-Location electron
npm run build
npx --no-install electron tools/companion-preview.cjs --interactive-check --verify-cross-display --exit-after-verify
Pop-Location
```

For a separately installed character bundle, set `AMADEUS_PREVIEW_CHARACTER_ROOT` to that local directory. For a specific Python installation, set `AMADEUS_PYTHON`. Neither path should be committed. Reports and screenshots stay under ignored `runtime/companion-preview/`.

The regression covers whole-card editing, persistence after reload, independent cross-display card/scene dragging, simultaneous readers, return menus and five-project parent endpoints. It uses Chromium pointer injection inside the isolated Electron renderer, not a claim of complete OS-level acceptance. The related daily Windows version received actual user confirmation that Codex's voice bar stays open during Companion interaction; that confirmation is distinct from this public-branch fixture run. Mixed-DPI, monitor hotplug during a drag and macOS/Linux behavior still need real-device qualification.

## 中文说明

这次是 Companion 功能分支的阶段性更新：单卡跨屏、各屏独立阅读、三种排列方式、明确的编辑模式、整卡拖动与自动保存、同级任务层叠、主对话节点和右键收回。

公开分支保留原有上游功能，以及默认不开启外部观察和提醒语音的设置。本轮没有把日常 develop 整体合入，也没有发布本地配置、真实任务记录、人物素材或模型权重。外部侧边任务的完整接入和长期美术设计仍是单独的工作范围。
