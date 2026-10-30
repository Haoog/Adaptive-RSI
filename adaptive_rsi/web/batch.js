'use strict';
window.RSIBatch=(()=>{
 const selected=new Map(),pending=new Set();
 let historyBox=null;
 const actionLabels={group:'合并任务',ungroup:'拆分任务',split_retry:'拆分失败任务组并重做',retry:'重试',cancel:'取消任务',approve:'批准交付'};
 const stateLabels={accepted:'已受理',unknown:'结果待核对',executing:'正在处理',grouped:'已合并',restored:'已恢复',failed:'操作失败',skipped:'已跳过'};
 async function history(){
  if(historyBox?.open)return;
  const project=projectId,data=await api('tasks/batches?project='+encodeURIComponent(project));
  const box=el('dialog',undefined,'experience-dialog');historyBox=box;
  box.append(el('h2',t('批次记录')));
  const filter=el('input');filter.type='search';filter.setAttribute('aria-label',t('搜索批次记录'));
  const rows=el('div');box.append(filter,rows);
  function renderRows(){
   rows.replaceChildren();
   for(const report of data.batches.filter(report=>JSON.stringify(report).toLowerCase().includes(filter.value.toLowerCase()))){
    const card=el('section'),title=el('p',t(actionLabels[report.action]||'未知操作')+' · '+(report.request_id||'')+' · '+(report.receipts||[]).map(item=>t(stateLabels[item.state]||'结果待核对')).join(', '));
    card.append(title,el('p',report.tasks.map(task=>task.title).join(' / ')));
    if(!report.complete&&report.request_id){
     const retry=el('button',t('继续并核对批次'),'button');retry.type='button';
     retry.onclick=()=>safe(async()=>{retry.disabled=true;try{const result=await api('tasks/batch',{project,tasks:report.tasks.map(task=>task.id),action:report.action,request_id:report.request_id,revision:report.revision,preview:false});Object.assign(report,result);renderRows();}finally{retry.disabled=false;}});
     card.append(retry);
    }
    rows.append(card);
   }
  }
  filter.oninput=renderRows;renderRows();
  const download=el('button',t('导出批次记录'),'button');download.type='button';
  download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),link=el('a');link.href=url;link.download='adaptive-rsi-batches-'+project+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  const close=el('button',t('关闭'),'text-button');close.type='button';close.onclick=()=>box.close();
  box.append(download,close);box.onclose=()=>{if(historyBox===box)historyBox=null;box.remove();};document.body.append(box);box.showModal();filter.focus();
 }
 function ids(){if(!selected.has(projectId))selected.set(projectId,new Set());return selected.get(projectId);}
 function costText(cost){
  const seconds=value=>Number.isFinite(value)?value.toFixed(1)+' s':t('未知');
  return t('验证成本估计')+' · '+t('分别验证')+': '+seconds(cost?.separate_seconds)+' · '+t('共享检查收益')+': '+seconds(cost?.shared_savings_seconds)+' · '+t('失败重做代价')+': '+seconds(cost?.failure_redo_seconds)+'\n'+t('估计不保证加速；实际 CI 对照数据不足，每组仍需完整门禁。');
 }
 async function recommendations(){
  const project=projectId,tasks=Array.from(ids()).sort();if(!project||!tasks.length||pending.has(project))return;
  pending.add(project);toolbar();
  try{
   const report=await api('tasks/batch',{project,tasks,action:'recommend',preview:true});
   if(!report.recommendations.length){toast(t('没有可推荐的任务簇，请核对范围与验证证据。'));return;}
   for(const cluster of report.recommendations){
    const titles=cluster.tasks.map(id=>report.tasks.find(task=>task.id===id)?.title||id).join('\n');
    if(!(await uiConfirm(t('确认合并此推荐任务簇？')+'\n'+titles+'\n'+costText(cluster.verification_cost))))continue;
    // Reuse the same receipt identity as ordinary batch operations so a lost
    // response can be reconciled instead of creating a second request.
    const signature=JSON.stringify(['group',cluster.tasks]),saved=window.RSIDraftStore?.get('batch',project);
    let prior=null;
    if(saved?.signature===signature){
     try{prior=(await api('tasks/batch?project='+encodeURIComponent(project)+'&id='+encodeURIComponent(saved.id))).receipt;}
     catch{prior=null;}
    }
    if(prior?.complete){window.RSIDraftStore?.remove('batch',project);for(const id of cluster.tasks)selected.get(project)?.delete(id);continue;}
    const request_id=saved?.signature===signature?saved.id:crypto.randomUUID();
    window.RSIDraftStore?.set('batch',project,{signature,id:request_id,tasks:cluster.tasks,action:'group'});
    const result=await api('tasks/batch',{project,tasks:cluster.tasks,action:'group',request_id,revision:cluster.revision,preview:false});
    if(result.complete){window.RSIDraftStore?.remove('batch',project);for(const id of cluster.tasks)selected.get(project)?.delete(id);}
    else toast(t('部分结果待核对，请打开批次记录。'));
   }
   await refresh({force:true});
  }finally{pending.delete(project);toolbar();}
 }
 async function evidence(task){
  const project=projectId,data=await api('task?id='+encodeURIComponent(task.id)+'&project='+encodeURIComponent(project));
  const mapping=data.group_lineage?.acceptance_evidence,box=el('dialog',undefined,'experience-dialog');
  box.append(el('h2',t('逐条验收证据')));
  box.append(el('p',t(mapping?.all_passed?'通过':'覆盖记录未知')));
  for(const item of mapping?.requirements||[]){
   const section=el('section');section.append(el('h3',item.task+' · '+(item.index+1)+' · '+item.criterion),el('p',t(item.state==='pass'?'通过':item.state==='fail'?'尚未通过':'未知')));
   for(const check of item.checks){const details=el('details');details.append(el('summary',check.run+' · '+check.stage+'['+check.index+'] · '+check.name),el('pre',JSON.stringify(check,null,2)));section.append(details);}
   box.append(section);
  }
  let saved={};try{saved=JSON.parse(data.evidence||'[]')[0]||{};}catch{}
  if(saved.verification_cost)box.append(el('p',costText(saved.verification_cost)));
  const actual=mapping?.actual_verification_cost?.separate_seconds;
  box.append(el('p',t('实际完整检查耗时')+' · '+(Number.isFinite(actual)?actual.toFixed(1)+' s':t('未知'))));
  const close=el('button',t('关闭'));close.onclick=()=>box.close();box.append(close);box.onclose=()=>box.remove();document.body.append(box);box.showModal();
 }
 function toolbar(){
  const host=document.querySelector('.task-toolbar');if(!host)return;
  let panel=document.getElementById('batch-actions');
  if(!panel){panel=el('div',undefined,'task-actions');panel.id='batch-actions';host.after(panel);}
  panel.dataset.selection=ids().size?'selected':'empty';
  panel.replaceChildren(el('span',t('已选择任务')+' · '+ids().size,'muted'));
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
  box.addEventListener('change',()=>{if(box.checked)ids().add(task.id);else ids().delete(task.id);toolbar();});
  if((task.fingerprint||'').startsWith('batch:')){const wrap=el('span');const button=el('button',t('逐条验收证据'),'text-button');button.type='button';button.onclick=()=>safe(()=>evidence(task));wrap.append(box,button);return wrap;}
  return box;
 }
 async function perform(kind){
  const project=projectId,tasks=Array.from(ids()).sort();if(!project||!tasks.length||pending.has(project))return;
  pending.add(project);toolbar();
  try{
  const signature=JSON.stringify([kind,tasks]),saved=window.RSIDraftStore?.get('batch',project);
  let prior=null;
  if(saved?.signature===signature)prior=(await api('tasks/batch?project='+encodeURIComponent(project)+'&id='+encodeURIComponent(saved.id))).receipt;
  if(prior?.complete){window.RSIDraftStore?.remove('batch',project);selected.get(project)?.clear();toolbar();toast(t('批量操作已完成'));await refresh({force:true});return;}
  const preview=prior||await api('tasks/batch',{project,tasks,action:kind,preview:true});
  const description=preview.tasks.map(item=>{
   const candidate=preview.candidates?.find(value=>value.task===item.id);
   const detail=candidate?'\n  '+t('风险')+': '+t(reviewNames[candidate.risk]||candidate.risk||'未知')+
    '\n  '+t('涉及文件')+': '+(candidate.files.join(', ')||t('未知'))+(candidate.files_truncated?' …':''):'';
   return item.title+' · '+t(statusNames[item.state]||item.state)+detail;
  }).join('\n');
  const conflict=preview.conflicts.length?t('存在重叠文件，请复核。')+'\n':'';
  const unknown=preview.unknown_scope.length?t('部分任务的文件范围未知，请先核对需求。')+'\n':'';
  const grouping=kind==='group'?t('原任务将合并为一个新任务并保留原始要求。')+'\n':kind==='split_retry'?t('原任务将恢复到队列，各自重新验证；任务组的失败证据继续保留。')+'\n'+preview.originals.map(item=>item.title).join('\n')+'\n'+(preview.supplement_required?t('分组后修订的完整要求将另建任务重新验证，避免丢失或猜测归属。')+'\n':''):'';
  if(!(await uiConfirm(t('确认对所选任务执行此操作？')+'\n'+grouping+(preview.verification_cost?costText(preview.verification_cost)+'\n':'')+conflict+unknown+description)))return;
  const request_id=saved?.signature===signature?saved.id:crypto.randomUUID();
  window.RSIDraftStore?.set('batch',project,{signature,id:request_id,tasks,action:kind});
  const result=await api('tasks/batch',{project,tasks,action:kind,request_id,revision:preview.revision,preview:false});
  if(result.complete)window.RSIDraftStore?.remove('batch',project);
  selected.get(project)?.clear();toolbar();
  toast(t(result.complete?'批量操作已完成':'部分结果待核对，请打开批次记录。')+' · '+result.receipts.filter(item=>['accepted','grouped','restored'].includes(item.state)).length+'/'+result.receipts.length);
  await refresh({force:true});
  }finally{pending.delete(project);toolbar();}
 }
 toolbar();
 return {row,toolbar,costText,evidence};
})();
