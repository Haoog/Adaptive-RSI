'use strict';
if(window.__TAURI_INTERNALS__){
 const invoke=(command,args={})=>window.__TAURI_INTERNALS__.invoke(command,args);
 const originalFetch=window.fetch.bind(window);
 let sshTunnelConnected=false;
 const ready=invoke('runtime_info').then(info=>{
  window.__RSI_MACHINE_KEY__=info.scope||'disconnected';
  sshTunnelConnected=info.kind==='remote'&&/^(?:ssh:|http:\/\/127\.0\.0\.1(?::|$))/.test(info.scope||'');
  return info;
 }).catch(error=>{console.warn(error);return null;});
 window.RSIDesktop={invoke,ready,isSshTunnel:()=>sshTunnelConnected};
 let switching=false,switched=false,remoteSessionExpired=false;
 const aborted=()=>new DOMException('Aborted','AbortError');
 function reloadRuntime(info,next){
  switching=switched=true;
  if(next?.scope&&next.scope!==info.scope)history.replaceState(null,'',location.pathname+location.search);
  location.reload();
 }
 window.fetch=async(input,options={})=>{
  if(typeof input!=='string'||!input.startsWith('/api/'))return originalFetch(input,options);
  const info=await ready;
  if(!info||switching||options.signal?.aborted)throw aborted();
  const id=crypto.randomUUID(),path=input.slice(5);
  const operation=invoke('request',{path,body:options.body?JSON.parse(options.body):null,requestId:id,connectionGeneration:info.connection_generation}).then(reply=>{
   if(switched)throw aborted();
   if(info.kind==='remote'&&!options.signal?.aborted&&reply.status===401&&path.split('?')[0]==='state')remoteSessionExpired=true;
   const headers={'Content-Type':'application/json'};
   if(typeof reply.requestId==='string'&&/^[a-f0-9]{16}$/.test(reply.requestId))headers['X-RSI-Request-ID']=reply.requestId;
   return new Response(reply.body,{status:reply.status,headers});
  },async error=>{
   if(error==='Runtime connection changed'){
    // Native recovery can change the connection outside the dialog. Reload
    // only once a replacement is available, without retrying the old request.
    if(!switching){
     const next=await invoke('runtime_info').catch(()=>null);
     if(!switching&&next?.connected&&next.connection_generation!==info.connection_generation)reloadRuntime(info,next);
    }
    throw aborted();
   }
   if(switched||error==='Request cancelled')throw aborted();
   throw error;
  });
  if(!options.signal)return operation;
  return new Promise((resolve,reject)=>{const abort=()=>{invoke('cancel_request',{id}).catch(()=>{});reject(aborted());};options.signal.addEventListener('abort',abort,{once:true});if(options.signal.aborted)abort();operation.then(resolve,reject).finally(()=>options.signal.removeEventListener('abort',abort));});
 };
 document.addEventListener('DOMContentLoaded',async()=>{
  const info=await ready,tools=document.querySelector('.project-tools');
  if(!info)return;
  const machine=document.createElement('button');machine.className='text-button';machine.textContent=info.name||'连接运行机器';tools.prepend(machine);
  const dialog=document.createElement('dialog'),form=document.createElement('form');
  const title=document.createElement('h2');title.textContent=t('运行机器');
  const modeLabel=document.createElement('label'),modeText=document.createElement('span');
  const mode=document.createElement('select');
  const httpsOption=document.createElement('option'),sshOption=document.createElement('option');
  httpsOption.value='https';sshOption.value='ssh';
  mode.append(httpsOption,sshOption);
  mode.value=sshTunnelConnected?'ssh':'https';
  modeLabel.append(modeText,mode);
  const hint=document.createElement('p');hint.id='desktop-connect-hint';
  const ownership=document.createElement('p');ownership.className='muted';ownership.hidden=info.kind!=='local'||info.owned;
  const compatibility=document.createElement('p');compatibility.className='muted';compatibility.hidden=true;
  const input=document.createElement('input');input.type='password';input.required=true;input.autocomplete='off';input.setAttribute('aria-describedby',hint.id);
  const error=document.createElement('p');error.className='form-error';error.setAttribute('role','alert');error.hidden=true;
  const connect=document.createElement('button');connect.type='submit';connect.className='button primary';connect.textContent=t('连接服务器');
  const local=document.createElement('button');local.type='button';local.className='button';local.textContent=t('使用本机');
  const close=document.createElement('button');close.type='button';close.className='text-button';close.textContent=t('关闭');close.onclick=()=>dialog.close();
  form.append(title,modeLabel,hint,ownership,compatibility,input,error,connect,local,close);dialog.append(form);document.body.append(dialog);
  let connectionError='',connectionDetail='';
  function showConnectionError(message,detail=''){
   connectionError=message;connectionDetail=detail;
   error.textContent=t(message)+(detail?' ('+detail+')':'');
   error.hidden=!message;
  }
  function updateConnectionMode(){
   const sshTunnel=mode.value==='ssh';
   hint.textContent=sshTunnel
    ?t('请先在终端建立 SSH 转发，并在使用期间保持终端运行。粘贴运行服务生成的 HTTP 认证地址，端口须与本机转发端口一致。')
    :t('连接服务器时粘贴 HTTPS 认证地址。账号和项目保留在运行机器，关闭客户端不会停止服务器任务。');
   input.placeholder=sshTunnel?'http://127.0.0.1:6090/?token=…':'https://rsi.example.com/?token=…';
  }
  mode.onchange=()=>{input.value='';showConnectionError('');updateConnectionMode();};
  async function change(args){
   if(connect.disabled)return;
   mode.disabled=connect.disabled=local.disabled=close.disabled=true;
   showConnectionError('');
   try{
    // Preflight uses the same parser as connect and leaves the current page's requests running.
    if(args.url!==null&&!await invoke('validate_connection_target',args)){
     showConnectionError(args.sshTunnel?'请输入使用 127.0.0.1 并带端口的 HTTP 认证地址。':'请输入 HTTPS 认证地址。');
     return;
    }
    if(switched)return;
    // Pause new requests while native validates the candidate. Old replies can still complete.
    switching=true;
    const next=await invoke('connect',args);
    input.value='';
    if(args.url===null&&next?.connected&&next.connection_generation===info.connection_generation){
     switching=false;
     dialog.close();
     return;
    }
    reloadRuntime(info,next);
   }catch(e){
    switching=switched;
    const message=String(e);
    if(args.sshTunnel){
     showConnectionError(message.includes('authentication')||message.includes('session')
      ?'SSH 隧道可达，但认证失败。请生成新的认证地址后重试。'
      :'未能通过 SSH 隧道连接。请检查终端中的转发仍在运行，并核对本机端口后重试。',
      /^(?:Error: )?Server connection failed$/.test(message)?'Server connection failed':'');
    }else showConnectionError(message);
   }finally{
    if(!switching)mode.disabled=connect.disabled=local.disabled=close.disabled=false;
   }
  }
  dialog.addEventListener('cancel',event=>{if(connect.disabled)event.preventDefault();});
  form.onsubmit=event=>{
   event.preventDefault();
   if(connect.disabled)return;
   const url=input.value.trim();
   if(!url){input.value='';input.reportValidity();return;}
   change(mode.value==='ssh'?{url,sshTunnel:true}:{url});
  };
  local.onclick=()=>change({url:null});
  function openMachine(){
   if(dialog.open)return;
   if(remoteSessionExpired)showConnectionError('远端登录已失效。请生成新的认证地址并重新连接。');
   dialog.showModal();
  }
  machine.onclick=openMachine;
  document.getElementById('reload-ui').addEventListener('click',event=>{
   if(!remoteSessionExpired)return;
   event.stopImmediatePropagation();
   openMachine();
  },true);
  if(info.kind==='remote'&&info.api_version===1){compatibility.hidden=false;compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');}
  dialog.addEventListener('close',()=>{if(!dialog.open)input.value='';});
  if(!info.connected){showConnectionError(info.error||'连接运行机器');dialog.showModal();}
  const folder=document.createElement('button');folder.type='button';folder.className='button';folder.textContent=t('选择文件夹');
  folder.onclick=()=>safe(async()=>{if(info.kind!=='local')throw Error(t('远端项目请填写服务器上的路径。'));const path=await invoke('choose_folder');if(path)document.getElementById('project-path').value=path;});
  document.getElementById('local-project-fields').append(folder);
  window.RSIDesktop.translate=()=>{
   machine.textContent=t(info.name||'连接运行机器');machine.title=t('运行机器');title.textContent=t('运行机器');
   modeText.textContent=t('服务器连接方式');httpsOption.textContent='HTTPS';sshOption.textContent=t('SSH 隧道');
   updateConnectionMode();showConnectionError(connectionError,connectionDetail);
   ownership.textContent=t('当前连接已有本机服务，退出软件不会停止它。');
   compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');
   input.setAttribute('aria-label',t('服务器认证地址'));connect.textContent=t('连接服务器');local.textContent=t('使用本机');close.textContent=t('关闭');folder.textContent=t('选择文件夹');
  };
  window.RSIDesktop.translate();
  document.addEventListener('click',event=>{const link=event.target.closest('a[href]');if(link&&!event.defaultPrevented&&!(link.hasAttribute?.('download')||link.download)&&/^https?:/.test(link.href)){event.preventDefault();invoke('open_url',{url:link.href}).catch(error=>toast(String(error),true));}});
 });
}
