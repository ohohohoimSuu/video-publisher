// L1: execute the production preview JavaScript in a synthetic DOM/canvas model.
// This is not a browser or a live platform acceptance test.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../vendor/social-auto-upload/utils/publisher_materials.py'), 'utf8');
const body = source.match(/script = """([\s\S]*?)"""/)[1];

function element({width=320, height=200, pixels=0, src='old', hidden=false, loaded=true, tainted=false, canvas=false}={}) {
  return {
    tagName: canvas ? 'CANVAS' : 'IMG', width, height, naturalWidth: width, naturalHeight: height,
    pixels, src, currentSrc: src, hidden, complete: loaded, tainted, isConnected: true,
    getClientRects() { return hidden ? [] : [this.getBoundingClientRect()]; },
    getBoundingClientRect() { return {width, height}; },
    getAttribute() { return null; },
  };
}
async function inspect(images, {busy=false, error=false, hidden=false}={}) {
  const root = element({hidden});
  root.querySelectorAll = selector => selector === 'img, canvas' ? images :
    selector.includes('role="progressbar"') ? (busy ? [element()] : []) :
    (error ? [{...element(), textContent:'synthetic failure'}] : []);
  images.forEach(image => image.parentElement=root);
  const context = {
    crypto: webcrypto, Uint8Array,
    getComputedStyle: el=>({display:el.hidden?'none':'block', visibility:'visible', opacity:'1'}),
    document: { createElement: () => {
      let image;
      return {getContext:()=>({
        drawImage: el=>{image=el;},
        getImageData: ()=>{
          if (image.tainted) throw new Error('synthetic unreadable pixels');
          return {data:new Uint8Array([image.pixels, image.pixels, image.pixels, 255])};
        },
      })};
    }},
  };
  return await vm.runInNewContext(`(${body})`, context)(root);
}

test('a different URL with identical pixels is not a new cover', async()=>{
  const before=await inspect([element()]);
  const after=await inspect([element({src:'blob:replacement'})]);
  assert.equal(before.signature, after.signature);
  assert.equal(after.ready,true);
});
test('source dimension changes alone do not count as new cover pixels',async()=>{
  assert.equal((await inspect([element()])).signature,
    (await inspect([element({width:640,height:400})])).signature);
});
test('new readable preview pixels create distinct evidence',async()=>{
  assert.notEqual((await inspect([element()])).signature,
    (await inspect([element({pixels:100})])).signature);
});
test('a changing smaller reference does not count as the main preview',async()=>{
  assert.equal((await inspect([element(),element({width:120,height:80,pixels:10})])).signature,
    (await inspect([element(),element({width:120,height:80,pixels:200})])).signature);
});
test('ambiguous large previews fail closed',async()=>{
  const result=await inspect([element(),element({pixels:10})]);
  assert.equal(result.ready,false);assert.equal(result.signature,null);
});
test('unreadable main preview cannot fall back to a readable reference',async()=>{
  const result=await inspect([element({tainted:true}),element({width:120,height:80,pixels:99})]);
  assert.equal(result.ready,false);assert.equal(result.signature,null);
});
test('hidden ancestor, loading, processing and error prevent completion',async()=>{
  for (const result of [await inspect([element()],{hidden:true}),await inspect([element({loaded:false})]),
    await inspect([element()],{busy:true}),await inspect([element()],{error:true})]) assert.equal(result.ready,false);
});
