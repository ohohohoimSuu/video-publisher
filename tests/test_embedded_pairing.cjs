const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../chrome-extension/page-bridge.js'),'utf8');
function content(origin='http://127.0.0.1:8765'){
 const calls=[],messages=[],handlers={};const window={addEventListener:(n,f)=>handlers[n]=f,postMessage:(m,o)=>messages.push([m,o])};window.top=window;
 const chrome={runtime:{sendMessage:async m=>{calls.push(m);return {ok:true,secret:'must-not-reach-page'};}}};
 vm.runInNewContext(source,{window,location:{origin},chrome,Set,setInterval(){}});return {window,calls,messages,handlers,origin};
}
test('embedded pairing rejects foreign pages, frames and invalid codes before contacting extension',async()=>{
 const foreign=content('https://untrusted.example');assert.equal(foreign.messages.length,0);assert.equal(foreign.handlers.message,undefined);
 const f=content();for(const e of [{source:{},origin:f.origin,data:{source:'publisher-page',type:'pair',requestId:'a',code:'a'.repeat(64)}},{source:f.window,origin:'https://wrong.example',data:{source:'publisher-page',type:'pair',requestId:'a',code:'a'.repeat(64)}},{source:f.window,origin:f.origin,data:{source:'publisher-page',type:'pair',requestId:'a',code:'bad'}}])await f.handlers.message(e);
 assert.equal(f.calls.length,0);
});
test('page receives pairing result only, never a stored secret, and needs no clipboard or popup',async()=>{
 const f=content();await f.handlers.message({source:f.window,origin:f.origin,data:{source:'publisher-page',type:'pair',requestId:'one',code:'a'.repeat(64)}});
 assert.equal(f.calls[0].type,'upload-pair');assert.equal(f.messages.at(-1)[0].ok,true);assert.equal(f.messages.at(-1)[0].requestId,'one');assert.ok(!JSON.stringify(f.messages).includes('must-not-reach-page'));
});
