// Offline analysis of the SpriteForge KTX2 pack: alpha bounds, transparent blocks,
// block/tile redundancy and BC7 transcode timing. Read-only; writes JSON to argv[3].
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const repo = path.resolve(process.argv[2] || ".");
const outPath = process.argv[3];
const stride = Number(process.argv[4] || 1); // analyse every Nth frame
const vendor = path.join(repo, "render/web/vendor");
const BASIS = require(path.join(vendor, "basis_transcoder.js"));
const root = path.join(repo, "assets/spriteforge/runtime/kurisu");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "runtime_manifest.json"), "utf8"));

const RGBA32 = 13, BC7 = 6;
const TILE = 64; // px

function hash(buf) { return crypto.createHash("md5").update(buf).digest("base64"); }

BASIS({ wasmBinary: fs.readFileSync(path.join(vendor, "basis_transcoder.wasm")) }).then((B) => {
  B.initializeBasis();
  const result = { stride, tile: TILE, clips: {} };
  let tTranscodeBc7 = 0, nBc7 = 0, tTranscodeRgba = 0;
  for (const [label, clip] of Object.entries(manifest.clips)) {
    const blockSet = new Set();
    const tileSet = new Set();
    let blocksTotal = 0, tilesTotal = 0, transparentBlocks = 0, transparentTiles = 0;
    let union = null;
    let w = 0, h = 0;
    const frameAreas = [];
    const frames = clip.frames.filter((_, i) => i % stride === 0 || i === clip.frames.length - 1);
    for (const rel of frames) {
      const data = new Uint8Array(fs.readFileSync(path.join(root, rel)));
      const f = new B.KTX2File(data);
      if (!f.isValid() || !f.startTranscoding()) throw new Error("bad ktx2 " + rel);
      w = f.getWidth(); h = f.getHeight();
      // BC7 (what the GPU actually stores)
      let t0 = process.hrtime.bigint();
      const bc7 = new Uint8Array(f.getImageTranscodedSizeInBytes(0, 0, 0, BC7));
      if (!f.transcodeImage(bc7, 0, 0, 0, BC7, 0, -1, -1)) throw new Error("bc7 fail " + rel);
      tTranscodeBc7 += Number(process.hrtime.bigint() - t0) / 1e6; nBc7++;
      // RGBA32 for alpha analysis
      t0 = process.hrtime.bigint();
      const rgba = new Uint8Array(f.getImageTranscodedSizeInBytes(0, 0, 0, RGBA32));
      if (!f.transcodeImage(rgba, 0, 0, 0, RGBA32, 0, -1, -1)) throw new Error("rgba fail " + rel);
      tTranscodeRgba += Number(process.hrtime.bigint() - t0) / 1e6;
      f.close(); f.delete();

      const bw = Math.ceil(w / 4), bh = Math.ceil(h / 4);
      // block redundancy within the clip (BC7 16-byte blocks)
      for (let b = 0; b < bw * bh; b++) {
        blockSet.add(hash(bc7.subarray(b * 16, b * 16 + 16)));
      }
      blocksTotal += bw * bh;
      // alpha bounds + transparent 4x4 blocks
      let minX = w, minY = h, maxX = -1, maxY = -1;
      const blockOpaque = new Uint8Array(bw * bh);
      for (let y = 0; y < h; y++) {
        const row = y * w * 4;
        for (let x = 0; x < w; x++) {
          if (rgba[row + x * 4 + 3] > 2) {
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
            blockOpaque[(y >> 2) * bw + (x >> 2)] = 1;
          }
        }
      }
      for (let b = 0; b < bw * bh; b++) if (!blockOpaque[b]) transparentBlocks++;
      if (maxX >= 0) {
        frameAreas.push(((maxX - minX + 1) * (maxY - minY + 1)) / (w * h));
        union = union
          ? [Math.min(union[0], minX), Math.min(union[1], minY), Math.max(union[2], maxX), Math.max(union[3], maxY)]
          : [minX, minY, maxX, maxY];
      }
      // 64px tile redundancy (hash the tile's RGBA pixels); skip fully transparent tiles
      const tw = Math.ceil(w / TILE), th = Math.ceil(h / TILE);
      for (let ty = 0; ty < th; ty++) {
        for (let tx = 0; tx < tw; tx++) {
          const hsh = crypto.createHash("md5");
          let anyAlpha = false;
          for (let y = ty * TILE; y < Math.min(h, (ty + 1) * TILE); y++) {
            const start = (y * w + tx * TILE) * 4;
            const end = (y * w + Math.min(w, (tx + 1) * TILE)) * 4;
            const seg = rgba.subarray(start, end);
            hsh.update(seg);
            if (!anyAlpha) for (let i = 3; i < seg.length; i += 4) if (seg[i] > 2) { anyAlpha = true; break; }
          }
          tilesTotal++;
          if (!anyAlpha) { transparentTiles++; continue; }
          tileSet.add(hsh.digest("base64"));
        }
      }
    }
    result.clips[label] = {
      frames: clip.frames.length,
      analysedFrames: frames.length,
      width: w, height: h,
      intervalMs: clip.frameIntervalMs,
      unionBBox: union,
      unionAreaFraction: union ? ((union[2] - union[0] + 1) * (union[3] - union[1] + 1)) / (w * h) : 0,
      meanFrameBBoxAreaFraction: frameAreas.reduce((a, b) => a + b, 0) / Math.max(1, frameAreas.length),
      transparentBlockFraction: transparentBlocks / blocksTotal,
      uniqueBlockFraction: blockSet.size / blocksTotal,
      transparentTileFraction: transparentTiles / tilesTotal,
      uniqueNonEmptyTileFraction: tileSet.size / tilesTotal,
    };
    process.stderr.write(`${label} done\n`);
  }
  result.meanBc7TranscodeMs = tTranscodeBc7 / nBc7;
  result.meanRgbaTranscodeMs = tTranscodeRgba / nBc7;
  fs.writeFileSync(outPath, JSON.stringify(result, null, 1));
  console.log("ok", nBc7, "frames analysed; mean BC7 transcode ms", result.meanBc7TranscodeMs.toFixed(2));
});
