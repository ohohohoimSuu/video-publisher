/* This script runs only on the two explicitly listed publisher origins.
 * Website code receives presence/status only, never the saved extension key. */
(() => {
  const ORIGINS=new Set(['http://127.0.0.1:8765','https://video-publisher.example.com']);
  if(window.top!==window||!ORIGINS.has(location.origin))return;
  const reply=value=>window.postMessage({source:'publisher-extension',...value},location.origin);
  window.addEventListener('message',async event=>{
    if(event.source!==window||event.origin!==location.origin||event.data?.source!=='publisher-page')return;
    const m=event.data;
    if(m.type==='hello'){
      reply({type:'ready'});
      chrome.runtime.sendMessage({type:'upload-connect'}).catch(()=>{});
    }
    if(m.type==='pair'&&typeof m.requestId==='string'&&m.requestId.length<100&&/^[a-f0-9]{64}$/.test(m.code||'')){
      try{
        const result=await chrome.runtime.sendMessage({type:'upload-pair',code:m.code});
        reply({type:'paired',requestId:m.requestId,ok:result?.ok===true});
      }catch{reply({type:'paired',requestId:m.requestId,ok:false});}
    }
  });
  reply({type:'ready'});
  let previous=null;
  setInterval(async()=>{
    try{
      const state=await chrome.runtime.sendMessage({type:'upload-status'});
      if(state?.connected!==previous){previous=state?.connected;reply({type:'connection',connected:previous===true});}
    }catch{}
  },3000);
})();
