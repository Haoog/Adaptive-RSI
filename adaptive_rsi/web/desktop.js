'use strict';
if(window.__TAURI_INTERNALS__){
 const invoke=(command,args={})=>window.__TAURI_INTERNALS__.invoke(command,args);
 const originalFetch=window.fetch.bind(window);
 let remoteRuntime=false,sshTunnelConnected=false;
 async function runtimeSnapshot(){
  let timer;
  try{
   return await Promise.race([
    invoke('runtime_info'),
    new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);}),
   ]);
  }catch{return null;}
  finally{clearTimeout(timer);}
 }
 const ready=(async()=>{
  try{
   // Apply only the winning snapshot. Once startup falls back to the connection
   // dialog, a late IPC reply cannot give this page a runtime or generation.
   const info=await runtimeSnapshot();
   if(info===null)return null;
   window.__RSI_MACHINE_KEY__=info.scope||'disconnected';
   remoteRuntime=info.kind==='remote';
   sshTunnelConnected=remoteRuntime&&/^(?:ssh:|http:\/\/127\.0\.0\.1(?::|$))/.test(info.scope||'');
   return info;
  }catch{return null;}
 })();
 let switching=false,switched=false,changing=false,remoteSessionExpired=false;
 let runtimeProbe=0,lastRuntimeReply=0,disconnectedGeneration=null,renderRuntimeConnection;
 const isLocalDisconnected=()=>disconnectedGeneration!==null;
 let stateRequest=0,lastSessionReply=0,clearConnectionExpiry;
 const localDisconnectedMessage='本机服务连接已中断。请选择“使用本机”重连，或连接服务器。';
 const sessionExpiredMessage='远端登录已失效。请生成新的认证地址并重新连接。';
 window.RSIDesktop={invoke,ready,isSshTunnel:()=>sshTunnelConnected,isLocalDisconnected,
  remoteSessionExpiry:()=>remoteRuntime?remoteSessionExpired:undefined};
 const aborted=()=>new DOMException('Aborted','AbortError');
 function observeRemoteSession(ticket,reply){
  if(ticket<=lastSessionReply)return;
  if(reply.status===401){lastSessionReply=ticket;remoteSessionExpired=true;return;}
  if(reply.status!==200)return;
  let snapshot;
  try{snapshot=JSON.parse(reply.body);}catch{return;}
  if(!Array.isArray(snapshot?.projects))return;
  // Only conclusive state replies count. A later failed or cancelled read cannot hide an earlier 401.
  lastSessionReply=ticket;
  if(!remoteSessionExpired)return;
  remoteSessionExpired=false;
  clearConnectionExpiry?.();
  window.RSIDesktop.onRemoteSessionRecovered?.(snapshot.build);
 }
 function reloadRuntime(info,next){
  switching=switched=true;
  if(!info||(next?.scope&&next.scope!==info.scope))history.replaceState(null,'',location.pathname+location.search);
  location.reload();
 }
 window.fetch=async(input,options={})=>{
  if(typeof input!=='string'||!input.startsWith('/api/'))return originalFetch(input,options);
  const info=await ready;
  if(!info||switching||options.signal?.aborted)throw aborted();
  const id=crypto.randomUUID(),path=input.slice(5);
  const ticket=info.kind==='remote'&&path.split('?')[0]==='state'&&
   String(options.method??'GET').toUpperCase()==='GET'&&options.body==null?++stateRequest:0;
  const operation=invoke('request',{path,body:options.body?JSON.parse(options.body):null,requestId:id,connectionGeneration:info.connection_generation}).then(reply=>{
   if(switched||options.signal?.aborted)throw aborted();
   const headers={'Content-Type':'application/json'};
   if(typeof reply.requestId==='string'&&/^[a-f0-9]{16}$/.test(reply.requestId))headers['X-RSI-Request-ID']=reply.requestId;
   const response=new Response(reply.body,{status:reply.status,headers});
   if(ticket)observeRemoteSession(ticket,reply);
   return response;
  },async error=>{
   const wasLocal=info.kind==='local'&&info.connected;
   const localUnavailable=wasLocal&&error==='Runtime unavailable';
   if(error==='Runtime connection changed'||localUnavailable){
    // Reload only once a replacement is available, without retrying the old request.
    if(localUnavailable&&options.signal?.aborted)throw aborted();
    let obsolete=switching||changing;
    if(!obsolete){
     const probe=++runtimeProbe;
     const next=await runtimeSnapshot();
     if(localUnavailable&&options.signal?.aborted)throw aborted();
     obsolete=switching||changing||probe<=lastRuntimeReply;
     const generation=next?.connection_generation;
     // An ordinary owned child exit clears the target without changing the generation.
     const confirmed=localUnavailable?Number.isSafeInteger(generation)&&(
      next.connected===false&&next.error==='Local runtime exited'&&generation===info.connection_generation||
      next.connected===true&&generation>info.connection_generation):typeof next?.connected==='boolean';
     if(!obsolete&&confirmed){
      lastRuntimeReply=probe;
      if(next.connected&&generation!==info.connection_generation&&
       (!isLocalDisconnected()||generation>disconnectedGeneration))reloadRuntime(info,next);
      if(wasLocal&&!next.connected&&Number.isSafeInteger(generation)){
       const first=!isLocalDisconnected();
       disconnectedGeneration=first?generation:Math.max(disconnectedGeneration,generation);
       if(first){renderRuntimeConnection?.();window.RSIDesktop.onLocalRuntimeDisconnected?.();}
      }
     }
    }
    const handled=wasLocal&&(obsolete||isLocalDisconnected()||switched);
    if(localUnavailable&&!handled)throw error;
    const failure=aborted();
    if(handled)failure.runtimeConnectionChanged=true;
    throw failure;
   }
   if(switched||error==='Request cancelled')throw aborted();
   throw error;
  });
  if(!options.signal)return operation;
  return new Promise((resolve,reject)=>{const abort=()=>{invoke('cancel_request',{id}).catch(()=>{});reject(aborted());};options.signal.addEventListener('abort',abort,{once:true});if(options.signal.aborted)abort();operation.then(resolve,reject).finally(()=>options.signal.removeEventListener('abort',abort));});
 };
 document.addEventListener('DOMContentLoaded',async()=>{
  const info=await ready,tools=document.querySelector('.project-tools');
  const machine=document.createElement('button');machine.className='text-button';tools.prepend(machine);
  const dialog=document.createElement('dialog'),form=document.createElement('form');
  const title=document.createElement('h2');title.id='desktop-connect-title';title.textContent=t('运行机器');
  dialog.setAttribute('aria-labelledby',title.id);
  const modeLabel=document.createElement('label'),modeText=document.createElement('span');
  const mode=document.createElement('select');
  const httpsOption=document.createElement('option'),sshOption=document.createElement('option');
  httpsOption.value='https';sshOption.value='ssh';
  mode.append(httpsOption,sshOption);
  mode.value=sshTunnelConnected?'ssh':'https';
  modeLabel.append(modeText,mode);
  const hint=document.createElement('p');hint.id='desktop-connect-hint';
  const ownership=document.createElement('p');ownership.className='muted';
  const compatibility=document.createElement('p');compatibility.className='muted';compatibility.hidden=true;
  const input=document.createElement('input');input.type='password';input.required=true;input.autocomplete='off';input.setAttribute('aria-describedby',hint.id);
  const error=document.createElement('p');error.className='form-error';error.setAttribute('role','alert');
  const connect=document.createElement('button');connect.type='submit';connect.className='button primary';connect.textContent=t('连接服务器');
  const local=document.createElement('button');local.type='button';local.className='button';local.textContent=t('使用本机');
  const close=document.createElement('button');close.type='button';close.className='text-button';close.textContent=t('关闭');close.onclick=()=>{if(!close.disabled)dialog.close();};
  const progress=document.createElement('p');progress.className='desktop-connection-progress';progress.setAttribute('role','status');
  const logs=document.createElement('button');logs.type='button';logs.className='button';
  const logError=document.createElement('p');logError.className='form-error';logError.setAttribute('role','alert');
  form.append(title,modeLabel,hint,ownership,compatibility,input,error,connect,local,close,progress,logs,logError);dialog.append(form);document.body.append(dialog);
  let connectionError='',connectionDetail='',connectionProgress='',returnFocus=machine,logFailed=false;
  function showProgress(message){connectionProgress=message;const text=t(message);if(progress.textContent!==text)progress.textContent=text;}
  function renderLogError(){logError.textContent=logFailed?t('无法打开本机日志目录，请稍后重试。'):'';}
  logs.onclick=async()=>{
   if(logs.disabled||logs.hidden)return;
   logs.disabled=true;logFailed=false;renderLogError();
   try{await invoke('open_runtime_log_directory');}
   catch{if(dialog.open){logFailed=true;renderLogError();}}
   finally{logs.disabled=false;}
  };
  function showConnectionError(message,detail=''){
   connectionError=message;connectionDetail=detail;
   const text=t(message)+(detail?' ('+detail+')':'');
   if(error.textContent!==text)error.textContent=text;
  }
  function updateConnectionMode(){
   const sshTunnel=mode.value==='ssh';
   hint.textContent=sshTunnel
    ?t('请先在终端建立 SSH 转发，并在使用期间保持终端运行。粘贴运行服务生成的 HTTP 认证地址，端口须与本机转发端口一致。')
    :t('连接服务器时粘贴 HTTPS 认证地址。账号和项目保留在运行机器，关闭客户端不会停止服务器任务。');
   input.placeholder=sshTunnel?'http://127.0.0.1:6090/?token=…':'https://rsi.example.com/?token=…';
  }
  mode.onchange=()=>{if(mode.disabled)return;input.value='';showConnectionError('');updateConnectionMode();};
  async function change(args){
   if(connect.disabled)return;
   changing=true;
   // A new choice supersedes probes that started before its preflight.
   lastRuntimeReply=runtimeProbe;
   mode.disabled=connect.disabled=local.disabled=close.disabled=input.readOnly=true;
   showConnectionError('');
   showProgress(args.url===null?'正在连接本机服务…':'正在核对认证地址…');
   try{
    // Preflight uses the same parser as connect and leaves the current page's requests running.
    if(args.url!==null&&!await invoke('validate_connection_target',args)){
     showConnectionError(args.sshTunnel?'请输入使用 127.0.0.1 并带端口的 HTTP 认证地址。':'请输入 HTTPS 认证地址。');
     return;
    }
    if(switched)return;
    // Pause new requests while native validates the candidate. Old replies can still complete.
    switching=true;
    if(args.url!==null)showProgress(args.sshTunnel?'正在通过 SSH 隧道连接…':'正在连接 HTTPS 服务…');
    const next=await invoke('connect',args);
    input.value='';
    if(args.url===null&&info?.kind==='local'&&info.connected&&!isLocalDisconnected()&&next?.connected&&next.connection_generation===info.connection_generation){
     switching=false;
     dialog.close();
     return;
    }
    reloadRuntime(info,next);
   }catch(e){
    switching=switched;
    const message=String(e);
    const checkRejected=/^(?:Error: )?Runtime (?:health|state) check failed \(HTTP 40[13]\)$/.test(message);
    if(args.sshTunnel){
     const authenticationFailed=checkRejected||/^(?:Error: )?(?:SSH tunnel authentication was rejected|Missing runtime authentication)$/.test(message);
     showConnectionError(authenticationFailed
      ?'SSH 隧道可达，但认证失败。请生成新的认证地址后重试。'
      :'未能通过 SSH 隧道连接。请检查终端中的转发仍在运行，并核对本机端口后重试。',
      /^(?:Error: )?Server connection failed$/.test(message)?'Server connection failed':'');
    }else if(args.url===null){
     showConnectionError(info&&!isLocalDisconnected()?message:'未能连接本机运行服务，请稍后重试。');
    }else if(checkRejected){
     showConnectionError('HTTPS 认证被拒绝。请生成新的认证地址后重试。');
    }else if(/^(?:Error: )?(?:Server connection failed|Cannot connect to the selected runtime)$/.test(message)){
     showConnectionError('无法连接 HTTPS 服务。请检查网络和服务器是否可达后重试。');
    }else if(/^(?:Error: )?Update the remote runtime before connecting$/.test(message)){
     showConnectionError('远端服务版本不兼容。请升级远端服务后重试。');
    }else{
     // Native uses its generic server rejection for any unexpected status on the initial request.
     showConnectionError('未能完成 HTTPS 连接。请核对完整的认证地址，并确认远端服务正在运行后重试。');
    }
   }finally{
    changing=false;
    if(!switching){
     mode.disabled=connect.disabled=local.disabled=close.disabled=input.readOnly=false;
     showProgress('');
     if(dialog.open)(args.url===null?local:input).focus();
    }
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
  clearConnectionExpiry=()=>{if(connectionError===sessionExpiredMessage)showConnectionError('');};
  renderRuntimeConnection=()=>{
   const disconnected=isLocalDisconnected();
   machine.textContent=t(disconnected?'重新连接运行机器':info?.name||'连接运行机器');
   ownership.hidden=disconnected||info?.kind!=='local'||info.owned;
   logs.hidden=!!info?.connected&&!disconnected;
   if(disconnected){
    document.getElementById('connection').textContent=t('连接中断');
    if(dialog.open&&!connectionError)showConnectionError(localDisconnectedMessage);
   }
  };
  function openMachine(opener){
   if(dialog.open)return;
   returnFocus=opener;
   if(!connectionError){
    if(isLocalDisconnected())showConnectionError(localDisconnectedMessage);
    else if(remoteSessionExpired)showConnectionError(sessionExpiredMessage);
   }
   dialog.showModal();
   mode.focus();
  }
  machine.onclick=()=>openMachine(machine);
  document.getElementById('reload-ui').addEventListener('click',event=>{
   if(!remoteSessionExpired)return;
   event.stopImmediatePropagation();
   openMachine(event.currentTarget||document.getElementById('reload-ui'));
  },true);
  if(info?.kind==='remote'&&info.api_version===1){compatibility.hidden=false;compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');}
  dialog.addEventListener('close',()=>{
   if(dialog.open)return;
   input.value='';logFailed=false;renderLogError();
   (returnFocus?.isConnected===false||returnFocus?.hidden||returnFocus?.disabled?machine:returnFocus)?.focus();
  });
  if(!info?.connected){
   showConnectionError(info?info.error||'连接运行机器':'无法读取当前运行机器。请连接服务器或使用本机。');
   openMachine(machine);
  }
  const folder=document.createElement('button');folder.type='button';folder.className='button';folder.textContent=t('选择文件夹');
  const projectDialog=document.getElementById('project-dialog'),projectForm=document.getElementById('project-form');
  const projectSource=document.getElementById('project-source'),projectPath=document.getElementById('project-path');
  folder.hidden=!info;
  const canChooseFolder=()=>info&&!switched&&projectDialog.open&&projectSource.value==='local';
  folder.onclick=()=>safe(async()=>{
   if(folder.disabled||!canChooseFolder())return;
   if(info.kind!=='local')throw Error(t('远端项目请填写服务器上的路径。'));
   const unchanged=unchangedForm(projectForm,{projectSensitive:false});
   const current=()=>canChooseFolder()&&unchanged();
   folder.disabled=true;
   try{
    const path=await invoke('choose_folder');
    if(current()&&path)projectPath.value=path;
   }catch(error){
    if(current())throw error;
   }finally{folder.disabled=false;}
  });
  document.getElementById('local-project-fields').append(folder);
  window.RSIDesktop.translate=()=>{
   renderRuntimeConnection();machine.title=t('运行机器');title.textContent=t('运行机器');
   modeText.textContent=t('服务器连接方式');httpsOption.textContent='HTTPS';sshOption.textContent=t('SSH 隧道');
   updateConnectionMode();showConnectionError(connectionError,connectionDetail);showProgress(connectionProgress);renderLogError();
   logs.textContent=t('打开本机日志目录');
   ownership.textContent=t('当前连接已有本机服务，退出软件不会停止它。');
   compatibility.textContent=t('远端服务为旧版 API。基础功能可继续使用；不支持的操作会提示升级服务。');
   input.setAttribute('aria-label',t('服务器认证地址'));connect.textContent=t('连接服务器');local.textContent=t('使用本机');close.textContent=t('关闭');folder.textContent=t('选择文件夹');
  };
  window.RSIDesktop.translate();
  document.addEventListener('click',event=>{const link=event.target.closest('a[href]');if(link&&!event.defaultPrevented&&!(link.hasAttribute?.('download')||link.download)&&/^https?:/.test(link.href)){event.preventDefault();invoke('open_url',{url:link.href}).catch(error=>toast(String(error),true));}});
 });
}
