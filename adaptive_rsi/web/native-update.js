'use strict';
window.RSINativeUpdate=(()=>{
 const labels={checking:'正在检查更新',latest:'已是最新稳定版',available:'发现新版本',downloading:'正在下载新版本…',verifying:'正在校验签名',replacing:'正在替换应用',ready:'新版本已准备好',waiting_idle:'等待当前任务结束后更新',installing:'正在安装更新',restarting:'正在重启应用',error:'更新未完成'};
 const errors={update_target_changed:'更新目标已变化或撤回，请重新检查并确认。',update_window_closed:'安装窗口已结束，下一次允许的空闲窗口会重新尝试。',updater_key_missing:'当前安装包尚未配置更新签名，请手动安装支持自动更新的版本。',update_disk_space:'磁盘可用空间不足，请清理后重试。',update_download_or_signature_failed:'下载或签名验证失败，当前版本继续可用。',update_wait_cancelled:'已取消等待更新，当前任务继续运行。',update_install_failed:'安装未完成，请重试或手动安装。',update_incompatible_platform:'此更新包与当前系统或架构不兼容，当前版本继续可用。',update_connect_timeout:'连接更新服务器超时，当前版本继续可用。请检查网络后重试。',update_response_timeout:'更新服务器响应超时，当前版本继续可用。',update_download_timeout:'下载新版本超过总时限，已停止。当前版本继续可用。',update_transfer_stalled:'下载长时间没有数据，已停止。当前版本继续可用。',update_connection_failed:'无法连接更新服务器，当前版本继续可用。',update_download_incomplete:'更新包下载不完整，已保留进度，可重试续传。',update_range_rejected:'服务器拒绝续传分段，已删除分段；请重试重新下载。'};
 let snapshot={},busy=false,checking=false,nodes=null,failures=0,retryAt=0,storageError=false,polling=false,installRetryAt=0,automaticInstall=false;
 let runtimeInfo=null,lastSample=null,currentSpeed=0;
 const failureCounts={};
 let policy=localRead('rsi-native-update-policy')||'download';
 let versionScope=localRead('rsi-native-update-scope')||'patch',installWindow=localRead('rsi-native-update-window')||'any';
 const enabled=()=>!!window.RSIDesktop;
 const preference=()=>policy;
 function persist(key,value){try{localStorage.setItem(key,value);return true;}catch{storageError=true;return false;}}
 function formatBytes(value){
  const size=Number(value);
  if(!Number.isFinite(size)||size<0)return '';
  if(size<1024)return size+' B';
  const units=['KB','MB','GB','TB'],index=Math.min(units.length-1,Math.floor(Math.log(size)/Math.log(1024))-1);
  return (size/1024**(index+1)).toFixed(index?1:0)+' '+units[index];
 }
 function speedText(value){
  const rate=Number(value);
  return Number.isFinite(rate)&&rate>0?formatBytes(rate)+'/s':'';
 }
 function phaseOf(snapshot){
  snapshot=snapshot||{};
  if(snapshot.state==='verifying')return 'verifying';
  if(snapshot.state==='downloading'){
   const total=Number(snapshot.total)||0,bytes=Number(snapshot.bytes)||0;
   // A fully transferred stream with a known size is waiting on signature
   // verification, even before the backend publishes the explicit stage.
   return snapshot.stage==='verifying'||snapshot.verifying===true||total>0&&bytes>=total?'verifying':'downloading';
  }
  return snapshot.state==='installing'||snapshot.state==='replacing'?'replacing':String(snapshot.state||'');
 }
 function progressText(snapshot){
  snapshot=snapshot||{};
  if(phaseOf(snapshot)!=='downloading')return '';
  const total=Number(snapshot.total),known=Number.isFinite(total)&&total>0,bytes=Number(snapshot.bytes)||0,parts=[];
  // An unknown or empty total must never produce a percentage or a guessed size.
  if(known)parts.push(Math.min(100,Math.round(bytes/total*100))+'%');
  parts.push(formatBytes(bytes)+(known?' / '+formatBytes(total):''));
  const speed=speedText(snapshot.speed!=null?snapshot.speed:snapshot.bytes_per_second);
  if(speed)parts.push(speed);
  return parts.join(' · ');
 }
 function reasonCount(reasons){
  if(Array.isArray(reasons))return reasons.length;
  const count=Number(reasons);
  return Number.isFinite(count)&&count>0?Math.floor(count):0;
 }
 function statusText(snapshot){
  snapshot=snapshot||{};
  const phase=phaseOf(snapshot);
  if(phase==='waiting_idle'){
   const count=reasonCount(snapshot.reasons);
   return count?t('等待 {count} 项本机任务结束后更新').replace('{count}',String(count)):t('等待当前任务结束后更新');
  }
  return t(labels[phase]||'桌面应用');
 }
 function scopeText(info){
  const neutral='更新本机客户端；当前无法判断服务影响范围。';
  if(!info||typeof info!=='object'||typeof info.kind!=='string')return neutral;
  if(info.kind==='remote')return '只更新本机客户端，远端服务不受影响。';
  if(info.kind==='local')return info.owned===true?'更新本机客户端并先排空自有服务任务。':'只更新本机客户端，不停止已有本机服务。';
  return neutral;
 }
 function errorText(code){return t(errors[code]||'更新未完成，请重试或手动安装。');}
 function sampleSpeed(previous,current,elapsed){
  const before=Number(previous),after=Number(current),ms=Number(elapsed);
  if(!Number.isFinite(before)||!Number.isFinite(after)||!Number.isFinite(ms)||ms<=0||after<before)return 0;
  return (after-before)*1000/ms;
 }
 function ensurePanel(){
  if(nodes)return;
  const panel=el('section',undefined,'native-update-panel');panel.id='native-update-panel';
  const status=el('p'),progress=el('p'),error=el('p',undefined,'form-error'),details=el('details'),summary=el('summary'),notes=el('pre');
  details.append(summary,notes);
  const label=el('label'),labelText=el('span'),select=el('select'),options=[];
  for(const [value,text] of [['manual','手动检查'],['download','后台下载，确认后重启'],['automatic','任务空闲时自动安装']]){
   const option=el('option');option.value=value;select.append(option);options.push([option,text]);
  }
  select.value=policy;
  select.onchange=()=>{policy=select.value;if(busy&&(policy==='manual'||automaticInstall&&policy!=='automatic'))window.RSIDesktop.invoke('native_update',{action:'cancel'}).catch(()=>{});storageError=false;persist('rsi-native-update-policy',policy);render();};
  label.append(labelText,select);
  const scopeLabel=el('label'),scopeText=el('span'),scopeSelect=el('select'),scopeOptions=[];
  for(const [value,text] of [['patch','仅补丁版本'],['minor','同主版本内更新'],['stable','所有稳定版本']]){const option=el('option');option.value=value;scopeSelect.append(option);scopeOptions.push([option,text]);}
  scopeSelect.value=versionScope;scopeSelect.onchange=()=>{versionScope=scopeSelect.value;persist('rsi-native-update-scope',versionScope);render();};scopeLabel.append(scopeText,scopeSelect);
  const windowLabel=el('label'),windowText=el('span'),windowSelect=el('select'),windowOptions=[];
  for(const [value,text] of [['any','任意空闲时间'],['night','本地夜间 22:00–06:00'],['weekend','本地周末']]){const option=el('option');option.value=value;windowSelect.append(option);windowOptions.push([option,text]);}
  windowSelect.value=installWindow;windowSelect.onchange=()=>{installWindow=windowSelect.value;persist('rsi-native-update-window',installWindow);render();};windowLabel.append(windowText,windowSelect);
  const buttons={};
  for(const [action,text] of [['check','检查更新'],['download','下载新版本'],['install','重启并更新'],['cancel','取消']]){
   const button=el('button',undefined,'button');button.type='button';
   button.onclick=()=>safe(()=>action==='cancel'?window.RSIDesktop.invoke('native_update',{action}):run(action));
   buttons[action]=[button,text];
  }
  const manual=el('a');manual.href='https://github.com/Haoog/Adaptive-RSI/releases/latest';manual.target='_blank';manual.rel='noreferrer';
  const receipt=el('button',undefined,'button');receipt.type='button';receipt.onclick=()=>safe(exportReceipt);
  const scope=el('p',undefined,'muted'),saved=el('p',undefined,'form-error');
  panel.append(status,progress,error,details,label,scopeLabel,windowLabel,scope,...Object.values(buttons).map(row=>row[0]),manual,receipt,saved);
  $('update-status').after(panel);
  nodes={panel,status,progress,error,details,summary,notes,labelText,select,options,buttons,manual,receipt,scope,saved,scopeText,scopeSelect,scopeOptions,windowText,windowSelect,windowOptions};
  bindRuntime();
 }
 function bindRuntime(){
  let source;try{source=window.RSIDesktop&&window.RSIDesktop.ready;}catch{source=null;}
  if(!source||typeof source.then!=='function')return;
  // Scope text must come from the live runtime_info; never infer ownership.
  source.then(info=>{runtimeInfo=info&&typeof info==='object'?info:null;render();},()=>{});
 }
 function observe(next){
  const now=Date.now(),bytes=Number(next&&next.bytes)||0,same=lastSample&&lastSample.version===(next&&next.version);
  currentSpeed=same?sampleSpeed(lastSample.bytes,bytes,now-lastSample.time):0;
  lastSample=next&&next.state==='downloading'?{version:next.version,bytes,time:now}:null;
  return currentSpeed;
 }
 function render(){
  if(!enabled())return;
  ensurePanel();
  const n=nodes,phase=phaseOf(snapshot);
  observe(snapshot);
  n.status.textContent=statusText(snapshot)+(snapshot.version?' · '+snapshot.version:'');
  n.progress.hidden=phase!=='downloading';
  n.progress.textContent=progressText(Object.assign({},snapshot,{speed:Number(snapshot.bytes_per_second)>0?snapshot.bytes_per_second:currentSpeed}));
  n.error.hidden=!snapshot.error_code;n.error.textContent=snapshot.error_code?errorText(snapshot.error_code):'';
  n.details.hidden=!snapshot.notes;n.summary.textContent=t('这个版本更新了什么');
  if(n.notes.textContent!==(snapshot.notes||''))n.notes.textContent=snapshot.notes||'';
  n.labelText.textContent=t('桌面更新策略');n.select.setAttribute('aria-label',t('桌面更新策略'));
  for(const [option,text] of n.options)option.textContent=t(text);
  n.scopeText.textContent=t('自动更新版本范围');n.scopeSelect.setAttribute('aria-label',t('自动更新版本范围'));
  n.windowText.textContent=t('自动安装时间');n.windowSelect.setAttribute('aria-label',t('自动安装时间'));
  for(const [option,text] of [...n.scopeOptions,...n.windowOptions])option.textContent=t(text);
  if(document.activeElement!==n.select)n.select.value=policy;
  n.scope.textContent=t(scopeText(runtimeInfo));
  for(const [action,[button,text]] of Object.entries(n.buttons)){
   button.textContent=t(text);button.disabled=action!=='cancel'&&busy;
   button.hidden=action==='download'?snapshot.state!=='available':action==='install'?snapshot.state!=='ready':action==='cancel'?!['downloading','waiting_idle'].includes(snapshot.state):false;
  }
  n.manual.textContent=t('手动下载安装包');
  n.receipt.textContent=t('导出升级回执');
  n.saved.hidden=!storageError;n.saved.textContent=t('偏好未保存到磁盘，本次会话已生效。');
 }
 function exportReceipt(){
  const receipt={receipt_version:1,generated_at:new Date().toISOString(),
   current_version:snapshot.current_version||'',target_version:snapshot.version||'',
   state:snapshot.state||'',error_code:snapshot.error_code||'',
   policy,version_scope:versionScope,install_window:installWindow,
   platform:(window.navigator&&navigator.platform)||'',locale:document.documentElement.lang||''};
  const blob=new Blob([JSON.stringify(receipt,null,2)],{type:'application/json'});
  const link=document.createElement('a');link.href=URL.createObjectURL(blob);
  link.download='adaptive-rsi-update-receipt.json';link.click();URL.revokeObjectURL(link.href);
  return receipt;
 }
 function eligible(current,target,scope=versionScope){
  const parse=value=>/^\d+\.\d+\.\d+$/.test(value||'')?value.split('.').map(Number):null,a=parse(current),b=parse(target);
  if(!a||!b||!['patch','minor','stable'].includes(scope))return false;
  if(b[0]<a[0]||b[0]===a[0]&&(b[1]<a[1]||b[1]===a[1]&&b[2]<=a[2]))return false;
  return scope==='stable'||b[0]===a[0]&&(scope==='minor'||b[1]===a[1]);
 }
 function withinWindow(date){
  if(installWindow==='any')return true;
  date=date||new Date();
  return installWindow==='night'?(date.getHours()>=22||date.getHours()<6):installWindow==='weekend'?[0,6].includes(date.getDay()):false;
 }
 async function run(action,automatic=false){
  if(busy)return;busy=true;automaticInstall=action==='install'&&automatic;render();
  try{
   snapshot=await window.RSIDesktop.invoke('native_update',{action,installationWindow:action==='install'?(automatic?installWindow:'any'):null});
   if(snapshot.state==='error')throw Error(snapshot.error_code||'update_check_failed');
   if(action==='install'){failureCounts.install=0;installRetryAt=0;}
   if(['check','download'].includes(action)){
    failureCounts[action]=0;if(snapshot.state==='ready')failureCounts.download=0;failures=Object.values(failureCounts).reduce((sum,value)=>sum+value,0);retryAt=0;persist('rsi-native-update-checked',String(Date.now()));
   }
  }catch(error){
   snapshot={state:'error',error_code:String(error)};
   failureCounts[action]=(failureCounts[action]||0)+1;failures=failureCounts[action];const next=Date.now()+Math.min(300000,5000*2**Math.min(failures-1,6));if(action==='install')installRetryAt=next;else retryAt=next;
  }finally{busy=false;automaticInstall=false;render();}
 }
 async function tick(){
  if(polling||!enabled())return;polling=true;
  try{snapshot=await window.RSIDesktop.invoke('native_update',{action:'status'});render();}
  catch(error){failures++;retryAt=Date.now()+Math.min(300000,5000*2**Math.min(failures-1,6));return;}
  finally{polling=false;}
  if(checking||busy||Date.now()<retryAt)return;checking=true;
  try{
   const last=Number(localRead('rsi-native-update-checked')||0);
   if(preference()!=='manual'&&((failureCounts.check||0)+(failureCounts.download||0)>0||Date.now()-last>6*3600000)){
    await run('check');
   }
   // A known available version must be downloaded even when the periodic check
   // is not due, e.g. after reopening the page or switching to download policy.
   const pendingDownload=!busy&&preference()!=='manual'&&snapshot.state==='available'&&eligible(snapshot.current_version,snapshot.version);
   // A successful feed check must not erase an active download backoff.
   if(snapshot.state!=='error'&&!pendingDownload){failureCounts.download=0;failures=Object.values(failureCounts).reduce((sum,value)=>sum+value,0);}
   if(pendingDownload)await run('download');
   if(!busy&&Date.now()>=installRetryAt&&snapshot.state==='ready'&&preference()==='automatic'&&(!snapshot.error_code||snapshot.error_code==='update_window_closed')&&eligible(snapshot.current_version,snapshot.version)&&withinWindow())await run('install',true);
  }finally{checking=false;}
 }

 document.addEventListener('DOMContentLoaded',()=>{if(!enabled())return;render();setTimeout(()=>safe(tick),5000);setInterval(()=>safe(tick),1000);});
 return {render,run,tick,eligible,withinWindow,phaseOf,statusText,progressText,formatBytes,speedText,sampleSpeed,scopeText,errorText};
})();
