'use strict';
const uploadStatus=document.getElementById('upload-status');
chrome.runtime.sendMessage({type:'upload-status'}).then(s=>{uploadStatus.textContent=s.connected?'普通 Chrome 已连接':'请回发布台点击连接 Chrome';}).catch(()=>{uploadStatus.textContent='请在扩展管理页重新加载';});
document.getElementById('upload-disconnect').addEventListener('click',async()=>{await chrome.runtime.sendMessage({type:'upload-disconnect'});uploadStatus.textContent='已断开，原有平台页面保留。';});
