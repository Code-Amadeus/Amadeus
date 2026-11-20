const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../render/web/wallpaper_scene.js'), 'utf8');
const crt = JSON.parse(fs.readFileSync(path.join(__dirname, '../wallpaper/crt_config.json'), 'utf8'));

function slice(from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `${from} ... ${to}`);
  return source.slice(start, end);
}

// The scene's own mapping methods, evaluated against a screen of the given size.
function scene(width, height) {
  const code = `${slice('function coverTransform(', 'function callRender(')}
    ({ app: { screen: { width: ${width}, height: ${height} } }, cfg: ${JSON.stringify(crt)},
       ${slice('_imageTransform() {', '_computeBounds(points) {')}
       ${slice('_scaledRect(rect) {', '_buildAmbientFromBackground(url) {')} })`;
  return vm.runInNewContext(code, {});
}

function bounds(points) {
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  return { x: Math.min(...xs), y: Math.min(...ys), width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) };
}

const [imageWidth, imageHeight] = crt.img_size;
const artCrt = bounds(crt.crt_polygon.map(([x, y]) => ({ x, y })));

for (const [width, height] of [[1920, 1080], [1920, 1200], [2560, 1600], [1280, 1024], [2560, 1080]]) {
  test(`${width}x${height}: the art covers the screen at its own aspect and the CRT stays whole`, () => {
    const s = scene(width, height);
    const sprite = {};
    s._placeBackdrop(sprite);
    assert.ok(Math.abs(sprite.width / sprite.height - imageWidth / imageHeight) < 1e-9);
    assert.ok(sprite.x <= 1e-9 && sprite.y <= 1e-9);
    assert.ok(sprite.x + sprite.width >= width - 1e-9 && sprite.y + sprite.height >= height - 1e-9);
    assert.ok(Math.abs(sprite.x + sprite.width / 2 - width / 2) < 1e-9);

    const screenCrt = bounds(s._crtPoints());
    assert.ok(Math.abs(screenCrt.width / screenCrt.height - artCrt.width / artCrt.height) < 1e-9);
    assert.ok(screenCrt.x >= 0 && screenCrt.y >= 0);
    assert.ok(screenCrt.x + screenCrt.width <= width && screenCrt.y + screenCrt.height <= height);

    const corner = crt.crt_polygon[0];
    assert.deepEqual(s._scaledPoint(corner), s._crtPoints()[0]);
    const rect = s._scaledRect([corner[0], corner[1], 10, 10]);
    assert.equal(rect.width, rect.height);
  });
}

test('on a 16:9 screen the art fills it exactly, as before', () => {
  const s = scene(1672 * 2, 941 * 2);
  const sprite = {};
  s._placeBackdrop(sprite);
  assert.deepEqual([sprite.x, sprite.y, sprite.width, sprite.height], [0, 0, 1672 * 2, 941 * 2]);
});
