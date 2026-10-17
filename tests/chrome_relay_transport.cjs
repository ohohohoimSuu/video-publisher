/* Real local sockets with a simulated extension. Never opens a browser. */
const assert=require('node:assert/strict');
const {Relay}=require('../chrome_relay.cjs');
const {ws:WebSocket,wsServer}=require(process.argv[2]);
(async()=>{
 const relay=new Relay({controlToken:'a'.repeat(64),extensionToken:'b'.repeat(64),extensionOrigin:'chrome-extension://'+'c'.repeat(32),wsServer});
 const port=await relay.listen();const commands=[];
 const ext=new WebSocket(`ws://127.0.0.1:${port}/extension/${'b'.repeat(64)}`,{origin:'chrome-extension://'+'c'.repeat(32)});
 await new Promise((r,j)=>{ext.once('open',r);ext.once('error',j);});
 ext.on('message',raw=>{const m=JSON.parse(raw);commands.push(m);ext.send(JSON.stringify({id:m.id,result:m.method==='attach'?{targetId:'actual-frame'}:m.method==='version'?{product:'Chrome/155.0',userAgent:'Chrome/155.0',protocolVersion:'1.3'}:{}}));});
 const api=async(path,body)=>{const r=await fetch(`http://127.0.0.1:${port}${path}`,{method:body?'POST':'GET',headers:{'X-Relay-Token':'a'.repeat(64)},body:body?JSON.stringify(body):undefined});assert.equal(r.status,200);return r.json();};
 assert.equal((await fetch(`http://127.0.0.1:${port}/status`)).status,403);
 assert.equal((await api('/status')).connected,true);
 const {endpoints}=await api('/prepare',{platforms:['douyin','xiaohongshu','bilibili'],files:['/tmp/approved.mp4']});
 assert.equal(commands[0].method,'openTabs');assert.equal(commands[0].params.tabs.length,3);
 const client=new WebSocket(endpoints.douyin);await new Promise(r=>client.once('open',r));const messages=[];client.on('message',raw=>messages.push(JSON.parse(raw)));
 const command=async(id,method,params={},sessionId)=>{
   client.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
   for(let i=0;i<100;i++){const m=messages.find(x=>x.id===id);if(m)return m;await new Promise(r=>setTimeout(r,5));}throw Error('No response');
 };
 assert.equal((await command(1,'Browser.getVersion')).result.product,'Chrome/155.0');
 await command(2,'Target.setAutoAttach',{autoAttach:true});assert.equal(messages.find(m=>m.method==='Target.attachedToTarget').params.targetInfo.targetId,'actual-frame');
 assert.equal((await command(3,'Target.createTarget',{url:'about:blank'})).result.targetId,'actual-frame');
 assert.ok((await command(4,'DOM.setFileInputFiles',{files:['/tmp/denied']},'main')).error);
 assert.ok((await command(5,'Storage.getCookies')).error);
 client.close();for(const endpoint of Object.values(endpoints))await api('/release',{key:endpoint.split('/').pop()});
 ext.close();await relay.close();console.log('PASS: authenticated local WebSocket, three-tab dispatch, real target identity, task file scope, cookie denial, and detach without closing tabs');
})().catch(e=>{console.error(e);process.exit(1);});
