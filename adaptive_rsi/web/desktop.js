'use strict';
if(window.__TAURI_INTERNALS__){
 const invoke=(command,args={})=>window.__TAURI_INTERNALS__.invoke(command,args);
 const originalFetch=window.fetch.bind(window);
 const ready=invoke('runtime_info').then(info=>{window.__RSI_MACHINE_KEY__=info.scope||'disconnected';return info;}).catch(error=>{console.warn(error);return null;});
 window.RSIDesktop={invoke,ready};
 window.fetch=async(input,options={})=>{
  if(typeof input!=='string'||!input.startsWith('/api/'))return originalFetch(input,options);
  await ready;if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
  const id=crypto.randomUUID();
  const operation=invoke('request',{path:input.slice(5),body:options.body?JSON.parse(options.body):null,requestId:id}).then(reply=>new Response(reply.body,{status:reply.status,headers:{'Content-Type':'application/json'}}));
  if(!options.signal)return operation;
  return new Promise((resolve,reject)=>{const abort=()=>{invoke('cancel_request',{id}).catch(()=>{});reject(new DOMException('Aborted','AbortError'));};options.signal.addEventListener('abort',abort,{once:true});if(options.signal.aborted)abort();operation.then(resolve,reject).finally(()=>options.signal.removeEventListener('abort',abort));});
 };
 document.addEventListener('DOMContentLoaded',async()=>{
  const info=await ready,tools=document.querySelector('.project-tools');
  if(!info)return;
  const machine=document.createElement('button');machine.className='text-button';machine.textContent=info.name||'连接运行机器';tools.prepend(machine);
  const dialog=document.createElement('dialog'),form=document.createElement('form');
  const title=document.createElement('h2');title.textContent=t('运行机器');
  const hint=document.createElement('p');hint.textContent=t('连接服务器时粘贴 HTTPS 认证地址。账号和项目保留在运行机器，关闭客户端不会停止服务器任务。');
  const ownership=document.createElement('p');ownership.className='muted';ownership.hidden=info.kind!=='local'||info.owned;
  const compatibility=document.createElement('p');compatibility.className='muted';compatibility.hidden=true;
  const input=document.createElement('input');input.type='password';input.autocomplete='off';input.placeholder='https://rsi.example.com/?token=…';input.setAttribute('aria-label',t('服务器认证地址'));
  const error=document.createElement('p');error.className='form-error';error.hidden=true;
  const connect=document.createElement('button');connect.type='submit';connect.className='button primary';connect.textContent=t('连接服务器');
  const local=document.createElement('button');local.type='button';local.className='button';local.textContent=t('使用本机');
  const close=document.createElement('button');close.type='button';close.className='text-button';close.textContent=t('关闭');close.onclick=()=>dialog.close();
  form.append(title,hint,ownership,compatibility,input,error,connect,local,close);dialog.append(form);document.body.append(dialog);
  async function change(url){connect.disabled=local.disabled=true;error.hidden=true;try{await invoke('connect',{url});input.value='';location.reload();}catch(e){error.textContent=String(e);error.hidden=false;}finally{connect.disabled=local.disabled=false;}}
  form.onsubmit=e=>{e.preventDefault();change(input.value.trim());};local.onclick=()=>change(null);machine.onclick=()=>dialog.showModal();
  if(info.kind==='remote'&&info.api_version===1){compatibility.hidden=false;compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');}
  if(!info.connected){error.textContent=info.error;error.hidden=false;dialog.showModal();}
  const folder=document.createElement('button');folder.type='button';folder.className='button';folder.textContent=t('选择文件夹');
  folder.onclick=()=>safe(async()=>{if(info.kind!=='local')throw Error(t('远端项目请填写服务器上的路径。'));const path=await invoke('choose_folder');if(path)document.getElementById('project-path').value=path;});
  document.getElementById('local-project-fields').append(folder);
  window.RSIDesktop.translate=()=>{
   machine.textContent=t(info.name||'连接运行机器');title.textContent=t('运行机器');
   hint.textContent=t('连接服务器时粘贴 HTTPS 认证地址。账号和项目保留在运行机器，关闭客户端不会停止服务器任务。');
   ownership.textContent=t('当前连接已有本机服务，退出软件不会停止它。');
   compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');
   input.setAttribute('aria-label',t('服务器认证地址'));connect.textContent=t('连接服务器');local.textContent=t('使用本机');close.textContent=t('关闭');folder.textContent=t('选择文件夹');
  };
  window.RSIDesktop.translate();
  document.addEventListener('click',event=>{const link=event.target.closest('a[href]');if(link&&/^https?:/.test(link.href)){event.preventDefault();invoke('open_url',{url:link.href}).catch(error=>toast(String(error),true));}});
 });
}
