/* Keeps every account in the user's ordinary Chrome profile. Only freshly
 * created job tabs can be attached. No enumeration/import of existing tabs. */
'use strict';
const LOCAL='http://127.0.0.1:8765';
const URLS={douyin:'https://creator.douyin.com/creator-micro/content/upload',xiaohongshu:'https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video',bilibili:'https://member.bilibili.com/platform/upload/video/frame'};
let socket=null,connecting=false;const jobs=new Map();
const allowed=(job,url)=>{try{const u=new URL(url);return u.protocol==='https:'&&u.hostname===new URL(URLS[job.platform]).hostname&&!u.port&&!u.username&&!u.password;}catch{return false;}};
const send=m=>{if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(m));};
async function detach(job){if(job.attached){job.attached=false;try{await chrome.debugger.detach({tabId:job.tabId});}catch{}}}
async function execute(method,p){
  if(method==='openTabs'){
    if(!Array.isArray(p.tabs)||!p.tabs.length||p.tabs.length>3||p.tabs.some(t=>URLS[t.platform]!==t.url||!/^[a-f0-9]{64}$/.test(t.grant)||jobs.has(t.grant)))throw Error('invalid_task');
    const made=[];
    try{
      for(const t of p.tabs){const tab=await chrome.tabs.create({url:t.url,active:true});jobs.set(t.grant,{...t,tabId:tab.id,attached:false});made.push(t.grant);}
      return {opened:made.length};
    }catch(e){for(const key of made)jobs.delete(key);throw e;}
  }
  const job=jobs.get(p.grant);if(!job)throw Error('task_tab_missing');
  if(method==='release'){await detach(job);jobs.delete(p.grant);return {};}
  const tab=await chrome.tabs.get(job.tabId);
  if(!allowed(job,tab.url||tab.pendingUrl))throw Error('platform_page_not_ready');
  if(method==='version')return {protocolVersion:'1.3',product:'Chrome/'+(navigator.userAgent.match(/Chrome\/([\d.]+)/)?.[1]||'0'),revision:'',userAgent:navigator.userAgent,jsVersion:''};
  if(method==='attach'){
    if(!job.attached){await chrome.debugger.attach({tabId:job.tabId},'1.3');job.attached=true;}
    const {frameTree}=await chrome.debugger.sendCommand({tabId:job.tabId},'Page.getFrameTree');
    return {targetId:frameTree.frame.id};
  }
  if(method==='cdp'){
    if(!job.attached)throw Error('debugger_detached');
    const target={tabId:job.tabId,...(p.sessionId?{sessionId:p.sessionId}:{})};
    return await chrome.debugger.sendCommand(target,p.method,p.params||{});
  }
  throw Error('unknown_command');
}
async function connect(){
  if(connecting||socket?.readyState===WebSocket.OPEN||socket?.readyState===WebSocket.CONNECTING)return;
  connecting=true;
  try{
    const {uploadKey}=await chrome.storage.local.get('uploadKey');if(!uploadKey)return;
    const response=await fetch(LOCAL+'/api/chrome/connect',{method:'POST',headers:{'Content-Type':'application/json','X-Chrome-Key':uploadKey},body:'{}',credentials:'omit',redirect:'error'});
    if(!response.ok)throw Error('local_service_unavailable');
    const {websocket}=await response.json();
    if(!/^ws:\/\/127\.0\.0\.1:\d+\/extension\/[a-f0-9]{64}$/.test(websocket))throw Error('invalid_endpoint');
    socket=new WebSocket(websocket);
    socket.onmessage=event=>{
      let m;try{m=JSON.parse(event.data);}catch{return;}
      if(!Number.isInteger(m.id))return;
      execute(m.method,m.params||{}).then(result=>send({id:m.id,result}),error=>send({id:m.id,error: ['task_tab_missing','platform_page_not_ready','debugger_detached','invalid_task'].includes(error.message)?error.message:'chrome_command_failed'}));
    };
    socket.onclose=()=>{socket=null;for(const job of jobs.values())detach(job);jobs.clear();};
    await new Promise((resolve,reject)=>{
      const opening=socket;
      const timer=setTimeout(()=>{opening.close();reject(Error('connection_timeout'));},8000);
      opening.onopen=()=>{clearTimeout(timer);resolve();};
      opening.onerror=()=>{clearTimeout(timer);reject(Error('connection_failed'));};
    });
  }finally{connecting=false;}
}
chrome.debugger.onEvent.addListener((source,method,params)=>{
  for(const [grant,job] of jobs)if(job.tabId===source.tabId&&job.attached)send({grant,event:{method,params,...(source.sessionId?{sessionId:source.sessionId}:{})}});
});
chrome.debugger.onDetach.addListener(source=>{
  for(const [grant,job] of jobs)if(job.tabId===source.tabId){job.attached=false;send({grant,event:{method:'Inspector.detached',params:{reason:'user_or_browser_detached'}}});}
});
chrome.tabs.onUpdated.addListener((id,change)=>{
  if(change.url)for(const job of jobs.values())if(job.tabId===id&&!allowed(job,change.url))detach(job);
});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message?.type==='upload-pair'){
    const origins=new Set(['http://127.0.0.1:8765','https://video-publisher.example.com']);
    let origin;try{origin=new URL(sender.tab?.url).origin;}catch{}
    if(!origins.has(origin)||sender.frameId!==0||!/^[a-f0-9]{64}$/.test(message.code||'')){reply({ok:false});return;}
    (async()=>{
      const response=await fetch(LOCAL+'/api/chrome/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:message.code}),credentials:'omit',redirect:'error'});
      const result=await response.json();if(!response.ok||!/^[a-f0-9]{64}$/.test(result.key||''))throw Error('pairing_failed');
      await chrome.storage.local.set({uploadKey:result.key});await connect();
      reply({ok:true});
    })().catch(()=>reply({ok:false}));
    return true;
  }
  if(message?.type==='upload-connect'){connect().then(()=>reply({ok:true}),()=>reply({ok:false}));return true;}
  if(message?.type==='upload-status'){reply({connected:socket?.readyState===WebSocket.OPEN});}
  if(message?.type==='upload-disconnect'){socket?.close();chrome.storage.local.remove('uploadKey').then(()=>reply({ok:true}));return true;}
});
chrome.alarms.create('publisher-reconnect',{periodInMinutes:0.5});
chrome.alarms.onAlarm.addListener(()=>connect().catch(()=>{}));
setInterval(()=>send({ping:true}),20000); // WebSocket traffic keeps the MV3 worker alive during jobs.
connect().catch(()=>{});
