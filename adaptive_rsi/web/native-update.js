'use strict';
window.RSINativeUpdate=(()=>{
 const labels={checking:'正在检查更新',latest:'已是最新稳定版',available:'发现新版本',downloading:'正在下载新版本…',ready:'新版本已准备好',waiting_idle:'等待当前任务结束后更新',installing:'正在安装更新',restarting:'正在重启应用',error:'更新未完成'};
 const errors={updater_key_missing:'当前安装包尚未配置更新签名，请手动安装支持自动更新的版本。',update_disk_space:'磁盘可用空间不足，请清理后重试。',update_download_or_signature_failed:'下载或签名验证失败，当前版本继续可用。',update_wait_cancelled:'已取消等待更新，当前任务继续运行。',update_install_failed:'安装未完成，请重试或手动安装。'};
 let snapshot={},busy=false,timer=null,checking=false;
 const enabled=()=>!!window.RSIDesktop;
 function preference(){return localRead('rsi-native-update-policy')||'download';}
 function render(){
  if(!enabled())return;
  let panel=document.getElementById('native-update-panel');
  if(!panel){panel=el('section',undefined,'native-update-panel');panel.id='native-update-panel';$('update-status').after(panel);}
  panel.replaceChildren(el('p',t(labels[snapshot.state]||'桌面应用')+(snapshot.version?' · '+snapshot.version:'')));
  if(snapshot.state==='downloading')panel.append(el('p',Math.round((snapshot.bytes||0)/1048576)+' MB'+(snapshot.total?' / '+Math.round(snapshot.total/1048576)+' MB':'')));
  if(snapshot.error_code)panel.append(el('p',t(errors[snapshot.error_code]||'更新未完成，请重试或手动安装。'),'form-error'));
  if(snapshot.notes){const details=el('details');details.append(el('summary',t('这个版本更新了什么')),el('pre',snapshot.notes));panel.append(details);}
  const label=el('label',t('桌面更新策略')),select=el('select');select.setAttribute('aria-label',t('桌面更新策略'));
  for(const [value,text] of [['manual','手动检查'],['download','后台下载，确认后重启'],['automatic','任务空闲时自动安装']]){const option=el('option',t(text));option.value=value;select.append(option);}
  select.value=preference();select.onchange=()=>{localWrite('rsi-native-update-policy',select.value);};label.append(select);panel.append(label);
  const actionButton=(text,action)=>{const button=el('button',t(text),'button');button.type='button';button.disabled=busy;button.onclick=()=>safe(()=>run(action));panel.append(button);};
  actionButton('检查更新','check');
  const manual=el('a',t('手动下载安装包'));manual.href='https://github.com/Haoog/Adaptive-RSI/releases/latest';manual.target='_blank';manual.rel='noreferrer';panel.append(manual);
  if(snapshot.state==='available')actionButton('下载新版本','download');
  if(snapshot.state==='ready')actionButton('重启并更新','install');
  if(['downloading','waiting_idle'].includes(snapshot.state)){const button=el('button',t('取消'),'button');button.type='button';button.onclick=()=>safe(()=>window.RSIDesktop.invoke('native_update',{action:'cancel'}));panel.append(button);}
 }
 async function run(action){
  if(busy)return;busy=true;render();
  try{snapshot=await window.RSIDesktop.invoke('native_update',{action});}
  catch(error){snapshot={state:'error',error_code:String(error)};}
  finally{busy=false;render();}
 }
 async function tick(){
  if(checking||!enabled())return;checking=true;
  try{
   snapshot=await window.RSIDesktop.invoke('native_update',{action:'status'});render();
   const last=Number(localRead('rsi-native-update-checked')||0);
   if(!busy&&preference()!=='manual'&&Date.now()-last>6*3600000){
    localWrite('rsi-native-update-checked',String(Date.now()));safe(()=>run('download'));
   }
   if(!busy&&snapshot.state==='ready'&&preference()==='automatic'&&!snapshot.error_code)safe(()=>run('install'));
  }finally{checking=false;}
 }
 document.addEventListener('DOMContentLoaded',()=>{if(!enabled())return;render();setTimeout(()=>safe(tick),5000);timer=setInterval(()=>safe(tick),1000);});
 return {render,run};
})();
