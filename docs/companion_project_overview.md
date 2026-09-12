# Companion project overview update

Date: 2026-09-12. Incremental presentation update to `codex/companion-prototype`, following the [multi-display milestone](companion_multidisplay_update.md).

## Behavior

Opening a project now shows all of its main and side conversations assigned to that display together. The expanded project no longer splits its tasks into pages or piles. The ordinary overview may still stack sibling cards; selecting one task opens its full reader, and returning restores the project map and then the saved overview.

Cards retain the supplied original Markdown, including paragraphs, lists and tables. The preview clips to the available card height; it does not replace the body with a generated summary or invoke another model. The existing source adapter determines what original content is available.

The map places whole conversation families, keeping side conversations near their actual parent. A connection to a later sibling routes around the card in between, so siblings are not presented as a false serial chain. Each display uses saved overview membership; reading never moves a remote task onto the current display.

The thumbnail rail is reduced from a maximum 244 to 156 logical pixels. It keeps the project label, count, a small tile per local task, status and parent connections; hover reveals the title and clicking opens the task. The old four-tile truncation is removed. A selected task is represented only once.

The layout first avoids the character footprint. If required, it may borrow a bounded outer edge, while reserving the central face/body area. The existing footprint is a layout approximation, not a face detector or a new character touch-region specification. Existing node transitions, skins, manual positions and source/voice settings remain in use.

## Capacity and qualification

| Expanded project | Observed on a 1080×1872 logical-pixel work area, with four other projects in the rail |
| --- | --- |
| 9 tasks: 5 main, 4 side | All visible at 240×188px with the original 14px body font |
| 15 tasks: 5 main, 10 side | All visible at the same card size, using the character's sides; visually denser |
| 25 tasks: 5 main, 20 side | Full relationship map fitted at roughly 35% scale; click to read, not a comfortable body-reading density |

The map has no fixed five-task limit. Geometry tests cover up to 100 tasks in one project, and rail regression covers five projects with 30 tasks each; these are not unlimited-load performance guarantees. Beyond readable capacity, the complete map is fitted rather than paginated or silently truncated. The overall project overview still shows five projects per group. Internal worker agents retain their existing parent-reader entry; this update does not broaden external side-chat observation coverage.

## Reproduce

With the optional character bundle and Python environment prepared:

```powershell
Push-Location electron
npm test
npm run build
npx --no-install electron tools/companion-preview.cjs --interactive-check --verify-project-map --exit-after-verify
Pop-Location
```

Set `AMADEUS_PREVIEW_CHARACTER_ROOT` and `AMADEUS_PYTHON` locally when using separate asset or Python installations. The harness requires two displays, uses the actual compiled renderer and fictional tasks, and makes no Codex, narration or model request. Reports and captures stay in ignored `runtime/companion-preview/`.

Validation for this public-branch integration is recorded in [Companion validation](companion_validation.md). Native pointer checks on the related daily implementation and compiled-renderer checks on this public branch are distinct evidence. Mixed-DPI, monitor removal during interaction, real speech and every external source format still require separate qualification.

## 中文说明

项目展开后，同屏主对话和侧边任务全部出现，取消项目内分页。原始正文按卡面高度显示，点击读取全文，不增加模型摘要。右侧缩略区更窄，每个本屏任务都保留自己的位置和连线；跨屏任务归属保持不变。

九张任务卡已覆盖主要复现场景，十五张明显更密，二十五张则进入整图缩放的关系查看模式。这里区分“程序能表示多少任务”和“同屏能舒服阅读多少任务”。人物主动推开卡片仍是后续美术交互方向，本次没有引入对应动画或自主移动行为。
