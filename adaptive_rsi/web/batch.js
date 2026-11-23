'use strict';
window.RSIBatch=(()=>{
 const selected=new Map(),pending=new Set(),boxes=new Map(),recommendationSelections=new Map();
 let historyDialog=null,recommendationDialog=null,evidenceRequest=null,visible=[],anchor=-1,pageKey='';
 function batchView(project){
  const machine=window.__RSI_MACHINE_KEY__||'',navigation=navigationSequence;
  return {machine,current:()=>project===projectId&&machine===(window.__RSI_MACHINE_KEY__||'')&&navigation===navigationSequence};
 }
 function viewChanged(){
  if(historyDialog?.box.open&&!historyDialog.current())historyDialog.box.close();
  if(recommendationDialog&&!recommendationDialog.current())recommendationDialog.controller.abort();
  if(evidenceRequest&&!evidenceRequest.viewCurrent()){
   const {box}=evidenceRequest;
   evidenceRequest=null;
   // 移除失效的模态框，避免原生 close() 把焦点归还给旧视图。
   box?.remove();
  }
 }
 async function confirmRecommendation(message,current){
  const dialog={current,controller:new AbortController()};
  recommendationDialog=dialog;
  try{return await uiConfirm(message,undefined,{signal:dialog.controller.signal});}
  finally{if(recommendationDialog===dialog)recommendationDialog=null;}
 }
 const actionLabels={group:'合并任务',ungroup:'拆分任务',split_retry:'拆分失败任务组并重做',retry:'重试',cancel:'取消任务',approve:'批准交付'};
 const stateLabels={accepted:'已受理',unknown:'结果待核对',executing:'正在处理',grouped:'已合并',restored:'已恢复',failed:'操作失败',skipped:'已跳过'};
 async function history(){
  if(historyDialog?.box.open)return;
  const project=projectId,{machine,current}=batchView(project);
  let data;
  try{data=await api('tasks/batches?project='+encodeURIComponent(project),undefined,{current});}
  catch(error){if(current())throw error;return;}
  if(!current()||historyDialog?.box.open)return;
  const box=el('dialog',undefined,'experience-dialog'),previousFocus=document.activeElement;
  const dialog={box,current},inFlight=new Set();historyDialog=dialog;
  const active=()=>box.open&&historyDialog===dialog&&current();
  box.append(el('h2',t('批次记录')));
  const filter=el('input');filter.type='search';filter.setAttribute('aria-label',t('搜索批次记录'));
  const rows=el('div');box.append(filter,rows);
  function renderRows(){
   rows.replaceChildren();
   for(const report of data.batches.filter(report=>JSON.stringify(report).toLowerCase().includes(filter.value.toLowerCase()))){
    const card=el('section'),title=el('p',t(actionLabels[report.action]||'未知操作')+' · '+(report.request_id||'')+' · '+(report.receipts||[]).map(item=>t(stateLabels[item.state]||'结果待核对')).join(', '));
    card.append(title,el('p',report.tasks.map(task=>task.title).join(' / ')));
    const request_id=report.request_id;
    if(!report.complete&&request_id){
     const retry=el('button',t('继续并核对批次'),'button');retry.type='button';
     retry.disabled=inFlight.has(request_id);
     retry.onclick=()=>safe(async()=>{
      if(!active()||report.complete||inFlight.has(request_id))return;
      inFlight.add(request_id);retry.disabled=true;
      try{
       const result=await api('tasks/batch',{project,tasks:report.tasks.map(task=>task.id),action:report.action,request_id,revision:report.revision,preview:false},{current:active});
       if(result.complete)forgetReceipt(project,request_id,machine);
       if(active())Object.assign(report,result);
      }catch(error){if(active())throw error;}
      finally{inFlight.delete(request_id);if(active())renderRows();}
     });
     card.append(retry);
    }
    rows.append(card);
   }
  }
  filter.oninput=renderRows;renderRows();
  const download=el('button',t('导出批次记录'),'button');download.type='button';
  download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),link=el('a');link.href=url;link.download='adaptive-rsi-batches-'+project+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  const close=el('button',t('关闭'),'text-button');close.type='button';close.onclick=()=>box.close();
  box.append(download,close);
  box.onclose=()=>{
   box.remove();
   if(historyDialog!==dialog)return;
   historyDialog=null;
   if(current()){
    const target=previousFocus?.isConnected&&previousFocus!==document.body?previousFocus:document.getElementById('batch-records');
    target?.focus();
   }
  };
  document.body.append(box);box.showModal();filter.focus();
 }
 function signature(action,tasks){return JSON.stringify([action,[...tasks].sort()]);}
 function receipts(project,machine){
  const saved=window.RSIDraftStore?.get('batch',project,machine);
  return Array.isArray(saved)?saved:saved?.signature?[saved]:[];
 }
 function findReceipt(project,action,tasks,machine){
  const key=signature(action,tasks);
  return receipts(project,machine).find(entry=>signature(entry.action,entry.tasks)===key);
 }
 function rememberReceipt(project,receipt,machine){
  const entries=receipts(project,machine);
  if(!entries.some(entry=>entry.id===receipt.id))window.RSIDraftStore?.set('batch',project,[...entries,receipt],machine);
 }
 function forgetReceipt(project,id,machine){
  const saved=receipts(project,machine),entries=saved.filter(entry=>entry.id!==id);
  if(entries.length===saved.length)return;
  if(entries.length)window.RSIDraftStore?.set('batch',project,entries,machine);
  else window.RSIDraftStore?.remove('batch',project,machine);
 }
 function ids(){if(!selected.has(projectId))selected.set(projectId,new Set());return selected.get(projectId);}
 function deselect(id){
  // 取消后重新勾选属于新选择，旧推荐回执不能清掉它。
  ids().delete(id);
  recommendationSelections.get(projectId)?.delete(id);
 }
// 选择只保留当前页仍可见的任务：筛选或翻页后不会再用用户看不见的 id 发起批量操作。
function syncPage(tasks){
 if(!Array.isArray(tasks))return;
 const idsOnPage=tasks.filter(task=>!(task.source||'').startsWith('system_')).map(task=>task.id),key=idsOnPage.join('\n');
 if(key!==pageKey){pageKey=key;anchor=-1;}
 visible=idsOnPage;
 const current=ids();
 for(const id of current)if(!visible.includes(id))deselect(id);
 for(const id of [...boxes.keys()])if(!visible.includes(id))boxes.delete(id);
}
function paint(){for(const [id,box] of boxes)box.checked=ids().has(id);}
 function costText(cost){
  const seconds=value=>Number.isFinite(value)?value.toFixed(1)+' s':t('未知');
  return t('验证成本估计')+' · '+t('分别验证')+': '+seconds(cost?.separate_seconds)+' · '+t('共享检查收益')+': '+seconds(cost?.shared_savings_seconds)+' · '+t('失败重做代价')+': '+seconds(cost?.failure_redo_seconds)+'\n'+t('估计不保证加速；实际 CI 对照数据不足，每组仍需完整门禁。');
 }
 async function recommendations(){
  const project=projectId,tasks=Array.from(ids()).sort();if(!project||!tasks.length||pending.has(project))return;
  const {machine,current}=batchView(project),requestOptions={current},originalSelection=new Set(tasks);
  recommendationSelections.set(project,originalSelection);
  const clearCompleted=cluster=>{
   for(const id of cluster.tasks)if(originalSelection.has(id))deselect(id);
  };
  pending.add(project);toolbar();
  try{
   const report=await api('tasks/batch',{project,tasks,action:'recommend',preview:true},requestOptions);
   if(!current())return;
   if(!report.recommendations.length){toast(t('没有可推荐的任务簇，请核对范围与验证证据。'));return;}
   for(const cluster of report.recommendations){
    const titles=cluster.tasks.map(id=>report.tasks.find(task=>task.id===id)?.title||id).join('\n');
    const confirmed=await confirmRecommendation(t('确认合并此推荐任务簇？')+'\n'+titles+'\n'+costText(cluster.verification_cost),current);
    if(!current())return;
    if(!confirmed)continue;
    const saved=findReceipt(project,'group',cluster.tasks,machine);
    let prior=null;
    if(saved){
     // 查询失败时沿用已经确认的请求编号，由服务端按回执核对。
     try{prior=(await api('tasks/batch?project='+encodeURIComponent(project)+'&id='+encodeURIComponent(saved.id),undefined,requestOptions)).receipt;}
     catch{prior=null;}
    }
    if(prior?.complete)forgetReceipt(project,saved.id,machine);
    if(!current())return;
    if(prior?.complete){clearCompleted(cluster);continue;}
    const request_id=saved?.id||crypto.randomUUID();
    rememberReceipt(project,{id:request_id,tasks:cluster.tasks,action:'group'},machine);
    const result=await api('tasks/batch',{project,tasks:cluster.tasks,action:'group',request_id,revision:cluster.revision,preview:false},requestOptions);
    if(result.complete)forgetReceipt(project,request_id,machine);
    if(!current())return;
    if(result.complete)clearCompleted(cluster);
    else toast(t('部分结果待核对，请打开批次记录。'));
   }
   await refresh({force:true});
  }catch(error){if(current())throw error;}
  finally{
   recommendationSelections.delete(project);pending.delete(project);
   if(project===projectId)toolbar();
  }
 }
 async function evidence(task,trigger=document.activeElement){
  const project=projectId,{current:viewCurrent}=batchView(project),previous=evidenceRequest;
  const request={viewCurrent,box:null};evidenceRequest=request;
  previous?.box?.remove();
  const current=()=>evidenceRequest===request&&viewCurrent();
  let data;
  try{data=await api('task?id='+encodeURIComponent(task.id)+'&project='+encodeURIComponent(project),undefined,{current});}
  catch(error){if(current()){evidenceRequest=null;throw error;}return;}
  if(!current())return;
  const mapping=data.group_lineage?.acceptance_evidence,box=el('dialog',undefined,'experience-dialog');
  request.box=box;
  const requirements=mapping?.requirements||[],positions=new Map();
  const checkIdentity=check=>JSON.stringify([check.run,check.stage,check.name]);
  for(const item of requirements)for(const check of item.checks||[]){
   const key=checkIdentity(check),indexes=positions.get(key)||new Set();
   indexes.add(check.index);positions.set(key,indexes);
  }
  const evidenceRows=requirements.map(item=>{
   const checks=item.checks||[],unambiguous=checks.filter(check=>positions.get(checkIdentity(check)).size===1);
   return {...item,checks:unambiguous,state:checks.length===unambiguous.length&&checks.length?item.state:'unknown'};
  });
  box.append(el('h2',t('逐条验收证据')));
  box.append(el('p',t(mapping?.all_passed&&evidenceRows.length&&evidenceRows.every(item=>item.state==='pass')?'通过':'覆盖记录未知')));
  for(const item of evidenceRows){
   const section=el('section');section.append(el('h3',item.task+' · '+(item.index+1)+' · '+item.criterion),el('p',t(item.state==='pass'?'通过':item.state==='fail'?'尚未通过':'未知')));
   for(const check of item.checks){const details=el('details');details.append(el('summary',check.run+' · '+check.stage+'['+check.index+'] · '+check.name),el('pre',JSON.stringify(check,null,2)));section.append(details);}
   box.append(section);
  }
  let saved={};try{saved=JSON.parse(data.evidence||'[]')[0]||{};}catch{}
  if(saved.verification_cost)box.append(el('p',costText(saved.verification_cost)));
  const actual=mapping?.actual_verification_cost?.separate_seconds;
  box.append(el('p',t('实际完整检查耗时')+' · '+(Number.isFinite(actual)?actual.toFixed(1)+' s':t('未知'))));
  const close=el('button',t('关闭'));close.onclick=()=>box.close();box.append(close);
  box.onclose=()=>{
   box.remove();
   if(!current())return;
   evidenceRequest=null;
   if(trigger?.isConnected)trigger.focus();
  };
  document.body.append(box);box.showModal();
 }
 function toolbar(tasks){
  syncPage(tasks);
  const host=document.querySelector('.task-toolbar');if(!host)return;
  let panel=document.getElementById('batch-actions');
  if(!panel){panel=el('div',undefined,'task-actions');panel.id='batch-actions';host.after(panel);}
  paint();
  const count=ids().size,page=visible.length,countLabel=el('span',t('当前页已选')+' · '+count+' / '+page,'muted');countLabel.setAttribute('role','status');
  panel.dataset.selection=count?'selected':'empty';
  panel.replaceChildren(countLabel);
  const selectAll=el('button',t('全选当前页'),'text-button');selectAll.id='batch-select-all';selectAll.type='button';selectAll.disabled=!page||count===page||pending.has(projectId);
  selectAll.addEventListener('click',()=>{for(const id of visible)ids().add(id);anchor=-1;toolbar();});panel.append(selectAll);
  const clear=el('button',t('清除选择'),'text-button');clear.type='button';clear.disabled=!count||pending.has(projectId);
  clear.addEventListener('click',()=>{ids().clear();anchor=-1;toolbar();});panel.append(clear);
  for(const [kind,label] of [['group','合并任务'],['ungroup','拆分未开始的任务'],['split_retry','拆分失败任务组并重做'],['retry','批量重试'],['cancel','批量取消'],['approve','批量批准交付']]){
   const button=el('button',t(label),'text-button');button.type='button';button.disabled=!ids().size||pending.has(projectId);
   button.addEventListener('click',()=>safe(()=>perform(kind)));panel.append(button);
  }
  const suggest=el('button',t('推荐任务簇'),'text-button');suggest.type='button';suggest.disabled=!ids().size||pending.has(projectId);suggest.onclick=()=>safe(recommendations);panel.append(suggest);
  const records=el('button',t('批次记录'),'text-button');records.id='batch-records';records.type='button';records.disabled=!projectId;records.onclick=()=>safe(history);panel.append(records);
 }
 function row(task){
  const box=el('input');box.type='checkbox';box.checked=ids().has(task.id);
  box.setAttribute('aria-label',t('选择任务')+' · '+task.title);
  boxes.set(task.id,box);
  // Shift+点击按当前页渲染顺序连选；普通点击只切换该行，并成为下一次连选的起点。
  box.addEventListener('click',event=>{
   const index=visible.indexOf(task.id);
   if(event.shiftKey&&anchor>=0&&index>=0&&anchor!==index){
    const [from,to]=anchor<index?[anchor,index]:[index,anchor];
    for(let position=from;position<=to;position++){if(box.checked)ids().add(visible[position]);else deselect(visible[position]);}
    toolbar();return;
   }
   if(box.checked)ids().add(task.id);else deselect(task.id);
   if(index>=0)anchor=index;
   toolbar();
  });
  if((task.fingerprint||'').startsWith('batch:')){const wrap=el('span');const button=el('button',t('逐条验收证据'),'text-button');button.type='button';button.onclick=()=>safe(()=>evidence(task,button));wrap.append(box,button);return wrap;}
  return box;
 }
 async function perform(kind){
  const project=projectId,tasks=Array.from(ids()).sort();if(!project||!tasks.length||pending.has(project))return;
  const {machine,current}=batchView(project),requestOptions={current};
  pending.add(project);toolbar();
  try{
   const saved=findReceipt(project,kind,tasks,machine);
   let prior=null,unverified=false;
   // 回执核对只用于恢复上一次已确认的请求；瞬时失败时保留原请求编号继续，由服务端按回执幂等复核。
   if(saved){
    try{prior=(await api('tasks/batch?project='+encodeURIComponent(project)+'&id='+encodeURIComponent(saved.id),undefined,requestOptions)).receipt;}
    catch{unverified=true;}
   }
   // 已完成的操作仍需清理原回执；界面反馈只属于发起时的导航。
   if(prior?.complete)forgetReceipt(project,saved.id,machine);
   if(!current())return;
   if(prior?.complete){selected.get(project)?.clear();toolbar();toast(t('批量操作已完成'));await refresh({force:true});return;}
   const preview=prior||await api('tasks/batch',{project,tasks,action:kind,preview:true},requestOptions);
   if(!current())return;
   const description=preview.tasks.map(item=>{
    const candidate=preview.candidates?.find(value=>value.task===item.id);
    const detail=candidate?'\n  '+t('风险')+': '+t(reviewNames[candidate.risk]||candidate.risk||'未知')+
     '\n  '+t('涉及文件')+': '+(candidate.files.join(', ')||t('未知'))+(candidate.files_truncated?' …':''):'';
    return item.title+' · '+t(statusNames[item.state]||item.state)+detail;
   }).join('\n');
   const conflict=preview.conflicts.length?t('存在重叠文件，请复核。')+'\n':'';
   const unknown=preview.unknown_scope.length?t('部分任务的文件范围未知，请先核对需求。')+'\n':'';
   const grouping=kind==='group'?t('原任务将合并为一个新任务并保留原始要求。')+'\n':kind==='split_retry'?t('原任务将恢复到队列，各自重新验证；任务组的失败证据继续保留。')+'\n'+preview.originals.map(item=>item.title).join('\n')+'\n'+(preview.supplement_required?t('分组后修订的完整要求将另建任务重新验证，避免丢失或猜测归属。')+'\n':''):'';
   const receiptNote=unverified?t('上次的批次回执暂时无法核对；继续会沿用原请求编号，服务端按回执返回结果，不会重复执行。')+'\n':'';
   if(!(await uiConfirm(t('确认对所选任务执行此操作？')+'\n'+receiptNote+grouping+(preview.verification_cost?costText(preview.verification_cost)+'\n':'')+conflict+unknown+description))||!current())return;
   const request_id=saved?.id||crypto.randomUUID();
   rememberReceipt(project,{id:request_id,tasks,action:kind},machine);
   let result;
   try{result=await api('tasks/batch',{project,tasks,action:kind,request_id,revision:preview.revision,preview:false},requestOptions);}
   catch(error){if(current())toast(t('批量操作结果待核对，原请求编号已保留；网络恢复后重新执行会先核对回执，不会重复操作。')+' · '+(error.message||''),true);return;}
   if(result.complete)forgetReceipt(project,request_id,machine);
   if(!current())return;
   selected.get(project)?.clear();toolbar();
   toast(t(result.complete?'批量操作已完成':'部分结果待核对，请打开批次记录。')+' · '+result.receipts.filter(item=>['accepted','grouped','restored'].includes(item.state)).length+'/'+result.receipts.length);
   await refresh({force:true});
  }catch(error){if(current())throw error;}
  finally{pending.delete(project);if(project===projectId)toolbar();}
 }
 toolbar();
 return {row,toolbar,costText,evidence,viewChanged};
})();
