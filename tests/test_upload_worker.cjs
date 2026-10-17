const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
function fixture(){
  const calls=[],tabs=new Map(),noop={addListener(){}};let id=200;
  const chrome={storage:{local:{get:async()=>({}),remove:async()=>{}}},tabs:{create:async p=>{const t={id:++id,url:p.url};tabs.set(t.id,t);calls.push(['create',p]);return t;},get:async id=>{assert.ok(tabs.has(id));return tabs.get(id);},onUpdated:noop},debugger:{attach:async t=>calls.push(['attach',t]),detach:async t=>calls.push(['detach',t]),sendCommand:async(t,m,p)=>{calls.push(['cdp',t,m,p]);return m==='Page.getFrameTree'?{frameTree:{frame:{id:'real-frame-id'}}}:{};},onEvent:noop,onDetach:noop},runtime:{onMessage:noop},alarms:{create(){},onAlarm:noop}};
  const context={chrome,console,URL,WebSocket:{OPEN:1},navigator:{userAgent:'Chrome/155.0.1'},setInterval(){}};
  const source=fs.readFileSync(path.join(__dirname,'../chrome-extension/upload-worker.js'),'utf8');
  vm.runInNewContext(source+'\nthis.worker={execute,jobs};',context);
  return {calls,tabs,worker:context.worker};
}
test('worker opens all three real Chrome task tabs without reading any existing tab or cookie',async()=>{
 const f=fixture();const urls={douyin:'https://creator.douyin.com/creator-micro/content/upload',xiaohongshu:'https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video',bilibili:'https://member.bilibili.com/platform/upload/video/frame'};
 await f.worker.execute('openTabs',{tabs:Object.entries(urls).map(([platform,url],i)=>({platform,url,grant:String(i).repeat(64)}))});
 assert.equal(f.calls.length,3);assert.ok(f.calls.every(c=>c[0]==='create'));assert.equal(f.tabs.size,3);
 const result=await f.worker.execute('attach',{grant:'0'.repeat(64)});assert.equal(result.targetId,'real-frame-id');
 await f.worker.execute('release',{grant:'0'.repeat(64)});assert.equal(f.tabs.size,3);assert.equal(f.worker.jobs.size,2);
});
test('worker refuses unknown tabs, mismatched platform URLs and redirected pages',async()=>{
 const f=fixture();await assert.rejects(f.worker.execute('cdp',{grant:'unknown',method:'Runtime.evaluate'}));
 await assert.rejects(f.worker.execute('openTabs',{tabs:[{grant:'a'.repeat(64),platform:'douyin',url:'https://unrelated.example'}]}));
 assert.equal(f.calls.length,0);
 await f.worker.execute('openTabs',{tabs:[{grant:'a'.repeat(64),platform:'douyin',url:'https://creator.douyin.com/creator-micro/content/upload'}]});
 f.tabs.get(201).url='https://unrelated.example';await assert.rejects(f.worker.execute('attach',{grant:'a'.repeat(64)}));assert.ok(!f.calls.some(c=>c[0]==='attach'));
});
