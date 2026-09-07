export const RENDER_BRIDGE_MESSAGE = 'amadeus.render.event'

export const RENDER_EVENT_METHODS = [
  'render.emotion',
  'render.speaking',
  'render.mouth',
  'render.subtitle',
  'render.sprite_frames',
  'render.mode',
  'render.idle_animation',
  'render.idle_frame_interval',
  'render.sprite_clip_config',
  'render.mouth_config',
  'render.spriteforge_graph',
  'render.spriteforge_intent',
  'render.spriteforge_release',
  'render.hold_frame',
  'render.clear_hold',
] as const

export function postRenderEvent(
  frame: HTMLIFrameElement | null,
  method: string,
  params: Record<string, unknown>,
): void {
  frame?.contentWindow?.postMessage({
    type: RENDER_BRIDGE_MESSAGE,
    method,
    params,
  }, '*')
}
