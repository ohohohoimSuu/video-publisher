/* Private loopback CDP transport for tabs created by our Chrome extension.
 * Uses Patchright's already-pinned websocket implementation; no public listener.
 * Existing tabs, browser-wide cookies and browser/profile management are absent.
 */
const http = require('node:http');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const PLATFORMS = Object.freeze({
  douyin: 'https://creator.douyin.com/creator-micro/content/upload',
  xiaohongshu: 'https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video',
  bilibili: 'https://member.bilibili.com/platform/upload/video/frame',
});
const validOrigin = origin => /^chrome-extension:\/\/[a-p]{32}$/.test(origin || '');
const same = (a,b) => typeof a==='string' && typeof b==='string' && a.length===b.length && crypto.timingSafeEqual(Buffer.from(a),Buffer.from(b));

class Relay extends EventEmitter {
  constructor({controlToken, extensionToken, extensionOrigin, wsServer}) {
    super(); Object.assign(this,{controlToken,extensionToken,extensionOrigin});
    this.pending=new Map();this.grants=new Map();this.serial=0;this.extension=null;
    this.server=http.createServer((req,res)=>this.http(req,res));
    this.wss=new wsServer({noServer:true,maxPayload:32*1024*1024,perMessageDeflate:false});
    this.server.on('upgrade',(req,socket,head)=>{
      const path=req.url;
      const ext=path==='/extension/'+this.extensionToken && req.headers.origin===extensionOrigin;
      const grant=path.startsWith('/cdp/') && this.grants.get(path.slice(5));
      if((!ext&&!grant)||(grant&&(grant.socket||req.headers.origin))){socket.destroy();return;}
      this.wss.handleUpgrade(req,socket,head,ws=>ext?this.attachExtension(ws):this.attachClient(ws,grant));
    });
  }
  async listen(){await new Promise(r=>this.server.listen(0,'127.0.0.1',r));return this.server.address().port;}
  async call(method,params={},timeout=20000){
    if(!this.extension||this.extension.readyState!==1)throw Error('extension_offline');
    const id=++this.serial;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('extension_timeout'));},timeout);
      this.pending.set(id,{resolve,reject,timer});this.extension.send(JSON.stringify({id,method,params}));
    });
  }
  attachExtension(ws){
    if(this.extension){ws.close(1008,'Already connected');return;}
    this.extension=ws;
    ws.on('message',raw=>{
      let m;try{m=JSON.parse(raw);}catch{return ws.close(1008);}
      if(m.id&&this.pending.has(m.id)){
        const p=this.pending.get(m.id);this.pending.delete(m.id);clearTimeout(p.timer);
        m.error?p.reject(Error(m.error)):p.resolve(m.result);return;
      }
      if(m.event&&typeof m.grant==='string'){
        const g=this.grants.get(m.grant);if(!g?.socket||g.socket.readyState!==1)return;
        const e={...m.event};
        if(e.method==='Target.attachedToTarget'&&e.params?.sessionId)g.sessions.add(e.params.sessionId);
        if(e.method==='Target.detachedFromTarget'&&e.params?.sessionId)g.sessions.delete(e.params.sessionId);
        e.sessionId=e.sessionId||'main';g.socket.send(JSON.stringify(e));
      }
    });
    ws.on('close',()=>{
      if(this.extension===ws)this.extension=null;
      for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(Error('extension_disconnected'));}this.pending.clear();
      for(const g of this.grants.values())g.socket?.close(1011,'Chrome extension disconnected');
    });
  }
  async http(req,res){
    const end=(code,v)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(v));};
    if(req.headers.origin||!same(req.headers['x-relay-token'],this.controlToken))return end(403,{error:'unauthorized'});
    let data='';for await(const chunk of req){data+=chunk;if(data.length>65536)return end(413,{error:'too_large'});}
    let p;try{p=data?JSON.parse(data):{};}catch{return end(400,{error:'bad_json'});}
    try{
      if(req.method==='GET'&&req.url==='/status')return end(200,{connected:this.extension?.readyState===1});
      if(req.method==='POST'&&req.url==='/prepare'){
        if(!Array.isArray(p.platforms)||!p.platforms.length||p.platforms.some(x=>!PLATFORMS[x])||new Set(p.platforms).size!==p.platforms.length)throw Error('invalid_platform');
        if(!Array.isArray(p.files)||p.files.some(x=>typeof x!=='string'||!x.startsWith('/')))throw Error('invalid_files');
        const grants=[];
        for(const platform of p.platforms){const key=crypto.randomBytes(32).toString('hex');const g={key,platform,files:new Set(p.files),sessions:new Set(['main']),targetId:'publisher-'+key,socket:null};this.grants.set(key,g);grants.push(g);}
        try{await this.call('openTabs',{tabs:grants.map(g=>({grant:g.key,platform:g.platform,url:PLATFORMS[g.platform]}))},30000);}
        catch(e){for(const g of grants){await this.release(g.key); }throw e;}
        return end(200,{endpoints:Object.fromEntries(grants.map(g=>[g.platform,'ws://127.0.0.1:'+this.server.address().port+'/cdp/'+g.key]))});
      }
      if(req.method==='POST'&&req.url==='/release'){await this.release(p.key);return end(200,{ok:true});}
      end(404,{error:'not_found'});
    }catch(e){end(503,{error:['extension_offline','extension_timeout','extension_disconnected','invalid_platform','invalid_files'].includes(e.message)?e.message:'chrome_bridge_failed'});}
  }
  async release(key){const g=this.grants.get(key);if(!g)return;this.grants.delete(key);g.socket?.close();try{await this.call('release',{grant:key});}catch{}}
  attachClient(ws,g){
    g.socket=ws;
    ws.on('message',raw=>{let m;try{m=JSON.parse(raw);}catch{return ws.close(1008);}
      this.command(g,m).then(result=>ws.readyState===1&&ws.send(JSON.stringify({id:m.id,result,...(m.sessionId?{sessionId:m.sessionId}:{})})),error=>ws.readyState===1&&ws.send(JSON.stringify({id:m.id,error:{code:-32000,message:error.message},...(m.sessionId?{sessionId:m.sessionId}:{})})));});
    ws.on('close',()=>{if(g.socket===ws)g.socket=null;});
  }
  async command(g,m){
    if(!Number.isInteger(m.id)||typeof m.method!=='string')throw Error('invalid_command');
    const p=m.params||{};
    if(!m.sessionId){
      switch(m.method){
        case 'Browser.getVersion':return this.call('version',{grant:g.key});
        case 'Browser.setDownloadBehavior':return {}; // Never change normal Chrome download settings.
        case 'Target.setDiscoverTargets':return {};
        case 'Target.getTargetInfo':return {targetInfo:{targetId:'publisher-browser',type:'browser',title:'',url:'',attached:true,browserContextId:'publisher-default'}};
        case 'Target.setAutoAttach':
          if(p.autoAttach&&!g.announced){
            const attached=await this.call('attach',{grant:g.key});
            if(typeof attached.targetId!=='string'||!attached.targetId)throw Error('target_identity_missing');
            g.targetId=attached.targetId;g.announced=true;
            g.socket.send(JSON.stringify({method:'Target.attachedToTarget',params:{sessionId:'main',waitingForDebugger:false,targetInfo:{targetId:g.targetId,type:'page',title:'',url:PLATFORMS[g.platform],attached:true,browserContextId:'publisher-default'}}}));
          }return {};
        case 'Target.createTarget':
          if(p.url!=='about:blank'||g.pageClaimed)throw Error('additional_tabs_not_allowed');
          g.pageClaimed=true;return {targetId:g.targetId};
        case 'Target.attachToTarget':
          if(p.targetId!==g.targetId)throw Error('unowned_target');
          return {sessionId:'main'};
        case 'Target.detachFromTarget':return {};
        default:throw Error('browser_command_not_allowed');
      }
    }
    if(!g.sessions.has(m.sessionId))throw Error('unowned_session');
    if(/^(Browser\.|Storage\.|ServiceWorker\.)/.test(m.method)||['Target.createTarget','Target.closeTarget','Target.createBrowserContext','Network.getAllCookies','Network.getCookies','Network.setCookie','Network.setCookies','Network.deleteCookies','Network.clearBrowserCookies','Page.close'].includes(m.method))throw Error('command_not_allowed');
    if(m.method==='DOM.setFileInputFiles'&&(!Array.isArray(p.files)||p.files.some(f=>!g.files.has(f))))throw Error('file_not_authorized');
    if(m.method==='Page.navigate'){
      const url=new URL(p.url);if(url.protocol!=='https:'||url.hostname!==new URL(PLATFORMS[g.platform]).hostname||url.port||url.username||url.password)throw Error('navigation_not_authorized');
    }
    return this.call('cdp',{grant:g.key,sessionId:m.sessionId==='main'?null:m.sessionId,method:m.method,params:p},60000);
  }
  async close(){for(const key of [...this.grants.keys()])await this.release(key);this.extension?.close();await new Promise(r=>this.server.close(r));this.wss.close();}
}
module.exports={Relay,PLATFORMS,validOrigin};
if(require.main===module){
  let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>{input+=chunk;if(input.includes('\n')){
    process.stdin.removeAllListeners('data');
    const config=JSON.parse(input.split('\n')[0]);if(!validOrigin(config.extensionOrigin))process.exit(2);
    const {wsServer}=require(config.bundlePath);const relay=new Relay({...config,wsServer});
    relay.listen().then(port=>process.stdout.write(JSON.stringify({port})+'\n'));
    process.stdin.on('end',()=>relay.close().finally(()=>process.exit()));
  }});
}
