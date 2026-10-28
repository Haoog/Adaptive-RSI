'use strict';
window.RSINativeUpdate=(()=>{
 const labels={checking:'正在检查更新',latest:'已是最新稳定版',available:'发现新版本',downloading:'正在下载新版本…',ready:'新版本已准备好',waiting_idle:'等待当前任务结束后更新',installing:'正在安装更新',restarting:'正在重启应用',error:'更新未完成'};
 const errors={updater_key_missing:'当前安装包尚未配置更新签名，请手动安装支持自动更新的版本。',update_disk_space:'磁盘可用空间不足，请清理后重试。',update_download_or_signature_failed:'下载或签名验证失败，当前版本继续可用。',update_wait_cancelled:'已取消等待更新，当前任务继续运行。',update_install_failed:'安装未完成，请重试或手动安装。'};
 let snapshot={},busy=false,checking=false,nodes=null,failures=0,retryAt=0,storageError=false;
 let policy=localRead('rsi-native-update-policy')||'download';
 let versionScope=localRead('rsi-native-update-scope')||'patch',installWindow=localRead('rsi-native-update-window')||'any';
 const enabled=()=>!!window.RSIDesktop;
 const preference=()=>policy;
 function persist(key,value){try{localStorage.setItem(key,value);return true;}catch{storageError=true;return false;}}
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
  select.onchange=()=>{policy=select.value;storageError=false;persist('rsi-native-update-policy',policy);render();};
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
  const scope=el('p',undefined,'muted'),saved=el('p',undefined,'form-error');
  panel.append(status,progress,error,details,label,scopeLabel,windowLabel,scope,...Object.values(buttons).map(row=>row[0]),manual,saved);
  $('update-status').after(panel);
  nodes={panel,status,progress,error,details,summary,notes,labelText,select,options,buttons,manual,scope,saved,scopeText,scopeSelect,scopeOptions,windowText,windowSelect,windowOptions};
 }
 function render(){
  if(!enabled())return;
  ensurePanel();
  const n=nodes;
  n.status.textContent=t(labels[snapshot.state]||'桌面应用')+(snapshot.version?' · '+snapshot.version:'');
  n.progress.hidden=snapshot.state!=='downloading';
  n.progress.textContent=Math.round((snapshot.bytes||0)/1048576)+' MB'+(snapshot.total?' / '+Math.round(snapshot.total/1048576)+' MB':'');
  n.error.hidden=!snapshot.error_code;n.error.textContent=snapshot.error_code?t(errors[snapshot.error_code]||'更新未完成，请重试或手动安装。'):'';
  n.details.hidden=!snapshot.notes;n.summary.textContent=t('这个版本更新了什么');
  if(n.notes.textContent!==(snapshot.notes||''))n.notes.textContent=snapshot.notes||'';
  n.labelText.textContent=t('桌面更新策略');n.select.setAttribute('aria-label',t('桌面更新策略'));
  for(const [option,text] of n.options)option.textContent=t(text);
  n.scopeText.textContent=t('自动更新版本范围');n.scopeSelect.setAttribute('aria-label',t('自动更新版本范围'));
  n.windowText.textContent=t('自动安装时间');n.windowSelect.setAttribute('aria-label',t('自动安装时间'));
  for(const [option,text] of [...n.scopeOptions,...n.windowOptions])option.textContent=t(text);
  if(document.activeElement!==n.select)n.select.value=policy;
  n.scope.textContent=t('更新本机客户端；仅自有本机服务会排空任务，远端服务不受影响。');
  for(const [action,[button,text]] of Object.entries(n.buttons)){
   button.textContent=t(text);button.disabled=action!=='cancel'&&busy;
   button.hidden=action==='download'?snapshot.state!=='available':action==='install'?snapshot.state!=='ready':action==='cancel'?!['downloading','waiting_idle'].includes(snapshot.state):false;
  }
  n.manual.textContent=t('手动下载安装包');
  n.saved.hidden=!storageError;n.saved.textContent=t('偏好未保存到磁盘，本次会话已生效。');
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
 async function run(action){
  if(busy)return;busy=true;render();
  try{
   snapshot=await window.RSIDesktop.invoke('native_update',{action});
   if(snapshot.state==='error')throw Error(snapshot.error_code||'update_check_failed');
   if(['check','download'].includes(action)){
    failures=0;retryAt=0;persist('rsi-native-update-checked',String(Date.now()));
   }
  }catch(error){
   snapshot={state:'error',error_code:String(error)};
   failures++;retryAt=Date.now()+Math.min(300000,5000*2**Math.min(failures-1,6));
  }finally{busy=false;render();}
 }
 async function tick(){
  if(checking||!enabled()||Date.now()<retryAt)return;checking=true;
  try{
   snapshot=await window.RSIDesktop.invoke('native_update',{action:'status'});render();
   const last=Number(localRead('rsi-native-update-checked')||0);
   if(!busy&&preference()!=='manual'&&(failures>0||Date.now()-last>6*3600000)){await run('check');if(snapshot.state==='available'&&eligible(snapshot.current_version,snapshot.version))await run('download');}
   if(!busy&&snapshot.state==='ready'&&preference()==='automatic'&&!snapshot.error_code&&eligible(snapshot.current_version,snapshot.version)&&withinWindow())await run('install');
  }catch(error){failures++;retryAt=Date.now()+Math.min(300000,5000*2**Math.min(failures-1,6));}
  finally{checking=false;}
 }
 document.addEventListener('DOMContentLoaded',()=>{if(!enabled())return;render();setTimeout(()=>safe(tick),5000);setInterval(()=>safe(tick),1000);});
 return {render,run,tick,eligible,withinWindow};
})();
