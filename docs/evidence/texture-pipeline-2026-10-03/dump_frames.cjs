// Decode selected KTX2 frames to raw RGBA files and time zstd vs transcode stages.
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const repo = path.resolve(process.argv[2] || "."), out = process.argv[3];
const vendor = path.join(repo, "render/web/vendor");
const BASIS = require(path.join(vendor, "basis_transcoder.js"));
const root = path.join(repo, "assets/spriteforge/runtime/kurisu");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "runtime_manifest.json"), "utf8"));
const picks = [
  ["idle", 0], ["idle", 60], ["speaking_long", 0], ["speaking_long", 180], ["smile_speaking", 90],
  ["idle_side_butterfly", 300], ["key_point_speaking", 90], ["idle_closed_eye", 369],
  ["thinking_speaking2", 150], ["shy_speaking2", 150], ["trans_smile", 60], ["surprise_speaking", 90],
];
fs.mkdirSync(out, { recursive: true });
BASIS({ wasmBinary: fs.readFileSync(path.join(vendor, "basis_transcoder.wasm")) }).then((B) => {
  B.initializeBasis();
  const list = [];
  for (const [label, idx] of picks) {
    const rel = manifest.clips[label].frames[idx];
    const data = new Uint8Array(fs.readFileSync(path.join(root, rel)));
    const f = new B.KTX2File(data);
    f.startTranscoding();
    const w = f.getWidth(), h = f.getHeight();
    const rgba = new Uint8Array(f.getImageTranscodedSizeInBytes(0, 0, 0, 13));
    f.transcodeImage(rgba, 0, 0, 0, 13, 0, -1, -1);
    f.close(); f.delete();
    const name = `${label}_${idx}.rgba`;
    fs.writeFileSync(path.join(out, name), rgba);
    list.push({ label, idx, w, h, file: name });
  }
  fs.writeFileSync(path.join(out, "frames.json"), JSON.stringify(list));

  // Stage timing over 60 frames of speaking_long: zstd-only vs full KTX2->BC7, and BC7+zstd size.
  const frames = manifest.clips.speaking_long.frames.slice(0, 60);
  let tZstd = 0, tFull = 0, uastcZ = 0, bc7Raw = 0, bc7Z19 = 0, bc7Z9 = 0, tBc7Unz = 0;
  for (const rel of frames) {
    const buf = fs.readFileSync(path.join(root, rel));
    // KTX2 level index: byteOffset/byteLength at 80, uncompressed length at 96
    const off = Number(buf.readBigUInt64LE(80)), len = Number(buf.readBigUInt64LE(88));
    let t0 = process.hrtime.bigint();
    zlib.zstdDecompressSync(buf.subarray(off, off + len));
    tZstd += Number(process.hrtime.bigint() - t0) / 1e6;
    uastcZ += len;
    const f = new B.KTX2File(new Uint8Array(buf));
    t0 = process.hrtime.bigint();
    f.startTranscoding();
    const bc7 = new Uint8Array(f.getImageTranscodedSizeInBytes(0, 0, 0, 6));
    f.transcodeImage(bc7, 0, 0, 0, 6, 0, -1, -1);
    tFull += Number(process.hrtime.bigint() - t0) / 1e6;
    f.close(); f.delete();
    bc7Raw += bc7.length;
    const z19 = zlib.zstdCompressSync(bc7, { params: { [zlib.constants.ZSTD_c_compressionLevel]: 19 } });
    const z9 = zlib.zstdCompressSync(bc7, { params: { [zlib.constants.ZSTD_c_compressionLevel]: 9 } });
    bc7Z19 += z19.length; bc7Z9 += z9.length;
    t0 = process.hrtime.bigint();
    zlib.zstdDecompressSync(z19);
    tBc7Unz += Number(process.hrtime.bigint() - t0) / 1e6;
  }
  const n = frames.length;
  console.log(JSON.stringify({
    frames: n,
    meanUastcZstdKiB: (uastcZ / n / 1024).toFixed(1),
    meanZstdDecodeMs: (tZstd / n).toFixed(2),
    meanFullTranscodeToBc7Ms: (tFull / n).toFixed(2),
    meanBc7RawKiB: (bc7Raw / n / 1024).toFixed(1),
    meanBc7Zstd19KiB: (bc7Z19 / n / 1024).toFixed(1),
    meanBc7Zstd9KiB: (bc7Z9 / n / 1024).toFixed(1),
    meanBc7Zstd19DecodeMs: (tBc7Unz / n).toFixed(2),
  }));
});
