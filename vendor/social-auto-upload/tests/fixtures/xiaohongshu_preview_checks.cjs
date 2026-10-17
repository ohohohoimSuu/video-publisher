// Execute the actual Python-embedded preview JavaScript against controlled
// DOM-shaped objects. This is L1 and deliberately does not claim real DOM QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
global.crypto = require('node:crypto').webcrypto;
global.getComputedStyle = (element) => ({display: 'block', visibility: 'visible', opacity: '1', ...element.style});
const inspect = eval(`(${JSON.parse(fs.readFileSync(0, 'utf8')).script})`);
function element({tag = 'IMG', width = 640, height = 360, pixels = [0, 0, 0, 255], ...extra} = {}) {
  return {tagName: tag, naturalWidth: width, naturalHeight: height, width, height,
    complete: true, pixels, style: {}, getClientRects: () => [1],
    getBoundingClientRect: () => ({width, height}), getAttribute: () => null, ...extra};
}
function root(elements = [], progress = []) {
  const value = element();
  value.querySelectorAll = (selector) => selector === 'img, canvas' ? elements : progress;
  return value;
}
global.document = {createElement: (tag) => {
  assert.equal(tag, 'canvas');
  const canvas = {width: 0, height: 0};
  let source;
  canvas.getContext = () => ({
    drawImage: (image) => {source = image;},
    getImageData: () => {
      if (source.tainted) throw new Error('Synthetic SecurityError');
      return {data: new Uint8ClampedArray(source.pixels)};
    },
  });
  return canvas;
}};
(async () => {
  const black = element({src: 'blob:old'});
  const old = await inspect(root([black]));
  assert.equal(old.ready, true);
  assert.ok(old.signature);
  assert.deepEqual(await inspect(root([element({src: 'blob:new'})])), old,
    'Changing a URL without changing pixels must not prove a new cover');
  assert.deepEqual(await inspect(root([element({width: 360, height: 640})])), old,
    'Changing dimensions without changing normalized pixels must not prove a new cover');
  const changed = await inspect(root([element({pixels: [0, 255, 0, 255]})]));
  assert.notEqual(changed.signature, old.signature);
  const unavailable = {signature: null, ready: false};
  assert.deepEqual(await inspect(root([element({complete: false})])), unavailable);
  assert.deepEqual(await inspect(root([element({tag: 'CANVAS', tainted: true}),
    element({width: 200, height: 100, pixels: [255, 0, 0, 255]})])), unavailable);
  assert.deepEqual(await inspect(root([black, element()])), unavailable, 'Ambiguous large previews must fail closed');
  assert.deepEqual(await inspect(root([black, element({width: 600, height: 320})])), unavailable,
    'Comparably large competing previews must also fail closed');
  const hiddenParent = element({style: {opacity: '0'}});
  assert.deepEqual(await inspect(root([black, element({width: 900, height: 500, parentElement: hiddenParent})])), old);
  assert.deepEqual(await inspect(root([black, element({width: 200, height: 100, pixels: [255, 0, 0, 255]})])), old,
    'A smaller reference image cannot stand in for the editing preview');
  assert.deepEqual(await inspect(root([black], [element()])), unavailable, 'Visible processing must block saving');
  assert.deepEqual(await inspect(root([black], [element({style: {display: 'none'}})])), old);
  const busyRoot = root([black]);
  busyRoot.getAttribute = (name) => name === 'aria-busy' ? 'true' : null;
  assert.deepEqual(await inspect(busyRoot), unavailable);
  assert.deepEqual(await inspect(root([])), unavailable);
  console.log('Controlled preview JavaScript checks passed (L1)');
})().catch((error) => {console.error(error); process.exitCode = 1;});
