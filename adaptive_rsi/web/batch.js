'use strict';
window.RSIBatch=(()=>{
 const selected=new Map();
 function ids(){if(!selected.has(projectId))selected.set(projectId,new Set());return selected.get(projectId);}
 function toolbar(){
  const host=document.querySelector('.task-toolbar');if(!host)return;
  let panel=document.getElementById('batch-actions');
  if(!panel){panel=el('div',undefined,'task-actions');panel.id='batch-actions';host.after(panel);}
  panel.replaceChildren(el('span',t('已选择任务')+' · '+ids().size,'muted'));
  for(const [kind,label] of [['group','合并任务'],['ungroup','拆分未开始的任务'],['retry','批量重试'],['cancel','批量取消'],['approve','批量批准交付']]){
   const button=el('button',t(label),'text-button');button.type='button';button.disabled=!ids().size;
   button.addEventListener('click',()=>safe(()=>perform(kind)));panel.append(button);
  }
 }
 function row(task){
  const box=el('input');box.type='checkbox';box.checked=ids().has(task.id);
  box.setAttribute('aria-label',t('选择任务')+' · '+task.title);
  box.addEventListener('change',()=>{if(box.checked)ids().add(task.id);else ids().delete(task.id);toolbar();});
  return box;
 }
 async function perform(kind){
  const project=projectId,tasks=Array.from(ids());if(!project||!tasks.length)return;
  const preview=await api('tasks/batch',{project,tasks,action:kind,preview:true});
  const description=preview.tasks.map(item=>{
   const candidate=preview.candidates?.find(value=>value.task===item.id);
   const detail=candidate?'\n  '+t('风险')+': '+t(reviewNames[candidate.risk]||candidate.risk||'未知')+
    '\n  '+t('涉及文件')+': '+(candidate.files.join(', ')||t('未知'))+(candidate.files_truncated?' …':''):'';
   return item.title+' · '+t(statusNames[item.state]||item.state)+detail;
  }).join('\n');
  const conflict=preview.conflicts.length?t('存在重叠文件，请复核。')+'\n':'';
  const unknown=preview.unknown_scope.length?t('部分任务的文件范围未知，请先核对需求。')+'\n':'';
  const grouping=kind==='group'?t('原任务将合并为一个新任务并保留原始要求。')+'\n':'';
  if(!(await uiConfirm(t('确认对所选任务执行此操作？')+'\n'+grouping+conflict+unknown+description)))return;
  const request_id=crypto.randomUUID();
  const result=await api('tasks/batch',{project,tasks,action:kind,request_id,revision:preview.revision,preview:false});
  selected.get(project).clear();toolbar();
  toast(t('批量操作已完成')+' · '+result.receipts.filter(item=>['accepted','grouped','restored'].includes(item.state)).length+'/'+result.receipts.length);
  await refresh({force:true});
 }
 return {row,toolbar};
})();
