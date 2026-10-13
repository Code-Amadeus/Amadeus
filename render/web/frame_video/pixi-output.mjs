// Uses the product's Pixi 7 build. VideoFrame is borrowed only during upload;
// the two output textures are independent of decoder/session ownership.
export function createVideoOutput(PIXI, renderer, layout, { separateAlpha = false } = {}) {
  const { width: w, height: h, codedWidth: cw, codedHeight: ch } = layout;
  class VideoResource extends PIXI.Resource {
    constructor() { super(cw, ch); this.frame = null; }
    upload(_renderer, base, texture) {
      if (!this.frame) throw Error('VideoFrame is unavailable for upload');
      const gl = _renderer.gl;
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, this.frame);
      texture.width = base.realWidth; texture.height = base.realHeight;
      return true;
    }
  }
  const resources = Array.from({ length: separateAlpha ? 2 : 1 }, () => new VideoResource());
  const inputs = resources.map(resource => new PIXI.Texture(new PIXI.BaseTexture(resource, {
    alphaMode: PIXI.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA, mipmap: PIXI.MIPMAP_MODES.OFF,
    scaleMode: PIXI.SCALE_MODES.NEAREST,
  })));
  const geometry = new PIXI.Geometry()
    .addAttribute('aVertexPosition', [0, 0, w, 0, w, h, 0, h], 2)
    .addAttribute('aTextureCoord', [0, 0, 1, 0, 1, 1, 0, 1], 2)
    .addIndex([0, 1, 2, 0, 2, 3]);
  const shader = PIXI.Shader.from(`
    precision highp float;
    attribute vec2 aVertexPosition;
    attribute vec2 aTextureCoord;
    uniform mat3 projectionMatrix;
    uniform mat3 translationMatrix;
    varying vec2 vUV;
    void main() {
      vUV = aTextureCoord;
      gl_Position = vec4((projectionMatrix * translationMatrix * vec3(aVertexPosition, 1.)).xy, 0., 1.);
    }`, `
    precision highp float;
    uniform sampler2D uColor;
    ${separateAlpha ? 'uniform sampler2D uAlpha;' : 'uniform float uAlphaX;'}
    uniform vec2 uScale;
    varying vec2 vUV;
    void main() {
      vec2 uv = vUV * uScale;
      vec3 color = texture2D(uColor, uv).rgb;
      float alpha = ${separateAlpha ? 'texture2D(uAlpha, uv).r' : 'dot(texture2D(uColor, uv + vec2(uAlphaX, 0.)).rgb, vec3(.2126, .7152, .0722))'};
      gl_FragColor = vec4(color * alpha, alpha);
    }`, { uColor: inputs[0], ...(separateAlpha ? { uAlpha: inputs[1] } : { uAlphaX: layout.alphaX / cw }),
    uScale: [w / cw, h / ch] });
  const state = PIXI.State.for2d(); state.blend = false;
  const mesh = new PIXI.Mesh(geometry, shader, state);
  const pool = Array.from({ length: 2 }, () => PIXI.RenderTexture.create({ width: w, height: h, resolution: 1 }));
  const retained = new Map(), live = new Set(pool);
  let displayed = null, held = false, destroyed = false, phase = 'initial';
  const emptyStats = () => ({ presents: 0, heldSkips: 0, noSpareSkips: 0, uploadErrors: 0 });
  const stats = emptyStats(); let scoped = emptyStats();
  const count = name => { stats[name]++; scoped[name]++; };
  const destroyTexture = texture => { if (live.delete(texture)) texture.destroy(true); };
  return {
    get texture() { return displayed; },
    hold(value) { held = Boolean(value); },
    setPhase(name) { phase = name; scoped = emptyStats(); },
    snapshot() {
      return { phase, total: { ...stats }, scoped: { ...scoped }, held, destroyed,
        liveOutputTextures: live.size, retainedOutputTextures: retained.size,
        liveUploadTextures: destroyed ? 0 : inputs.length,
        borrowedVideoFrames: resources.filter(resource => resource.frame !== null).length,
        // Nominal RGBA texel storage only. Driver/video surfaces are not measured here.
        nominalOutputBytes: live.size * w * h * 4 };
    },
    retain(texture = displayed) {
      if (!texture || !live.has(texture)) throw Error('Output texture is not live');
      retained.set(texture, (retained.get(texture) ?? 0) + 1);
      let released = false;
      return () => {
        if (released) return; released = true;
        const remaining = retained.get(texture) - 1;
        if (remaining) retained.set(texture, remaining);
        else { retained.delete(texture); if (destroyed) destroyTexture(texture); }
      };
    },
    present(frame, alphaFrame = null) {
      if (destroyed) throw Error('Output is destroyed');
      if (separateAlpha && (!alphaFrame || frame.timestamp !== alphaFrame.timestamp)) {
        throw Error('Color and alpha frame identities must match');
      }
      if (held) { count('heldSkips'); return null; }
      const next = pool.find(texture => texture !== displayed && !retained.has(texture));
      if (!next) { count('noSpareSkips'); return null; }
      const start = performance.now();
      resources[0].frame = frame;
      if (separateAlpha) resources[1].frame = alphaFrame;
      try {
        for (let i = 0; i < resources.length; i++) {
          resources[i].update(); renderer.texture.bind(inputs[i].baseTexture);
        }
        const uploaded = performance.now();
        renderer.render(mesh, { renderTexture: next, clear: true });
        displayed = next; count('presents');
        return { texture: next, uploadMs: uploaded - start, unpackSubmitMs: performance.now() - uploaded };
      } catch (error) { count('uploadErrors'); throw error; }
      finally { for (const resource of resources) resource.frame = null; }
    },
    destroy() {
      if (destroyed) return; destroyed = true;
      mesh.destroy({ children: true }); geometry.destroy(); shader.destroy();
      for (const input of inputs) input.destroy(true);
      for (const texture of pool) if (!retained.has(texture)) destroyTexture(texture);
      displayed = null;
    },
  };
}

// Read calibration.py color cells (rows 24..47) and optional alpha cells
// (rows 48..71), independently of timestamps. Historical fixtures lack the alpha
// marker flag and retain the color-only check. Center samples avoid edge ringing.
export function readCalibrationIdentity(pixels, width, height, { alphaIdentityMarker = false } = {}) {
  if (width !== 256 || height !== 127 || pixels.length !== width * height * 4) {
    throw Error('Unexpected calibration pixel dimensions');
  }
  let sourceIndex = 0;
  const cells = [];
  for (let bit = 0; bit < 5; bit++) {
    let sum = 0, alphaMin = 255, count = 0;
    for (let y = 32; y < 40; y++) for (let x = bit * 32 + 12; x < bit * 32 + 20; x++) {
      const offset = (y * width + x) * 4;
      sum += (pixels[offset] + pixels[offset + 1] + pixels[offset + 2]) / 3;
      alphaMin = Math.min(alphaMin, pixels[offset + 3]); count++;
    }
    const mean = sum / count, value = mean <= 40 ? 0 : mean >= 215 ? 1 : null;
    cells.push({ bit, mean, alphaMin, value });
    if (value === null || alphaMin < 245) return { sourceIndex: null, valid: false, cells };
    sourceIndex += value * 2 ** bit;
  }
  if (!alphaIdentityMarker) return { sourceIndex, valid: true, cells };
  let alphaSourceIndex = 0, alphaValid = true;
  const alphaCells = [];
  for (let bit = 0; bit < 5; bit++) {
    let sum = 0, count = 0;
    for (let y = 56; y < 64; y++) for (let x = bit * 32 + 12; x < bit * 32 + 20; x++) {
      sum += pixels[(y * width + x) * 4 + 3]; count++;
    }
    const mean = sum / count, value = mean <= 40 ? 0 : mean >= 215 ? 1 : null;
    alphaCells.push({ bit, mean, value });
    if (value === null) alphaValid = false;
    else alphaSourceIndex += value * 2 ** bit;
  }
  if (!alphaValid) alphaSourceIndex = null;
  const alphaMatchesColor = alphaValid && alphaSourceIndex === sourceIndex;
  return { sourceIndex, valid: alphaMatchesColor, cells, alphaSourceIndex, alphaCells, alphaMatchesColor };
}
