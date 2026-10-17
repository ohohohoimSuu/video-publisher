const test=require('node:test'),assert=require('node:assert/strict');
const {Relay}=require('../chrome_relay.cjs');
function relay(){const r=Object.create(Relay.prototype);r.call=async(method,params)=>({method,params});return r;}
function grant(){return {key:'a'.repeat(64),platform:'douyin',files:new Set(['/tmp/approved.mp4']),sessions:new Set(['main']),targetId:'one',socket:{send(){}}};}
test('gateway never exposes browser cookies, existing tabs, or profile controls',async()=>{
 const r=relay(),g=grant();
 for(const method of ['Storage.getCookies','Target.getTargets','Browser.close','Target.createBrowserContext','Network.getAllCookies'])await assert.rejects(r.command(g,{id:1,method}));
 for(const method of ['Storage.getCookies','Network.getAllCookies','Network.getCookies','Page.close','Target.closeTarget'])await assert.rejects(r.command(g,{id:1,sessionId:'main',method}));
 await assert.rejects(r.command(g,{id:1,sessionId:'foreign',method:'Runtime.evaluate',params:{expression:'1'}}));
});
test('gateway scopes file input to task material paths and navigation to its one platform',async()=>{
 const r=relay(),g=grant();
 await assert.rejects(r.command(g,{id:1,sessionId:'main',method:'DOM.setFileInputFiles',params:{files:['/private/file']}}));
 const result=await r.command(g,{id:2,sessionId:'main',method:'DOM.setFileInputFiles',params:{files:['/tmp/approved.mp4']}});assert.equal(result.method,'cdp');
 for(const url of ['https://creator.douyin.com.evil.test','file:///tmp/private','https://creator.xiaohongshu.com/','https://creator.douyin.com:999/'])await assert.rejects(r.command(g,{id:3,sessionId:'main',method:'Page.navigate',params:{url}}));
 await r.command(g,{id:4,sessionId:'main',method:'Page.navigate',params:{url:'https://creator.douyin.com/creator-micro/content/upload'}});
});
test('new_page is bound to the pre-opened task page and cannot enumerate or create extra tabs',async()=>{
 const r=relay(),g=grant();const got=await r.command(g,{id:1,method:'Target.createTarget',params:{url:'about:blank'}});assert.equal(got.targetId,'one');
 await assert.rejects(r.command(g,{id:2,method:'Target.createTarget',params:{url:'about:blank'}}));
 await assert.rejects(r.command(g,{id:3,method:'Target.attachToTarget',params:{targetId:'user-existing-tab'}}));
});
