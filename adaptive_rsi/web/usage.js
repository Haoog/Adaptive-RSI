'use strict';
window.RSIUsage=(()=>{
 let offset=0,sequence=0,detailSequence=0,report=null,timer=null;
 const number=value=>Number(value||0).toLocaleString(language);
 const token=value=>number(value.tokens)+(value.unknown_calls?' + '+t('部分未知'):'');
 const seconds=value=>Math.round(value||0)+' '+t('秒');
 function range(){const days=$('usage-period').value;if(days!=='custom')return {start:days==='all'?0:Date.now()/1000-Number(days)*86400,end:Date.now()/1000};const start=new Date($('usage-start').value).getTime()/1000,end=new Date($('usage-end').value).getTime()/1000+86400;if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)throw Error(t('请选择有效的日期范围。'));return {start,end};}
 function parameters(){return new URLSearchParams({...range(),project:$('usage-scope').value==='all'?'':projectId||'',q:$('usage-search').value,sort:$('usage-sort').value,offset,limit:25});}
 async function load(){
  if(view!=='usage')return;
  const ticket=++sequence;let unsupported=false;$('usage-refresh').disabled=true;
  try{const result=await api('usage?'+parameters());if(ticket!==sequence||view!=='usage')return;report=result;render();}
  catch(error){
   if(ticket!==sequence||view!=='usage')return;
   if(error.status!==404)throw error;
   unsupported=true;report=null;
   for(const id of ['usage-totals','usage-chart','usage-cost','usage-page'])$(id).replaceChildren();
   $('usage-tasks').replaceChildren(el('p',t('当前服务版本尚不支持用量页面，请先升级服务后再刷新。'),'muted'));
   $('usage-prev').disabled=$('usage-next').disabled=true;
  }finally{if(ticket===sequence)$('usage-refresh').disabled=false;clearTimeout(timer);if(view==='usage'&&!unsupported)timer=setTimeout(()=>safe(load),15000);}
 }
 function metric(label,value,note){const node=el('div',undefined,'usage-metric');node.append(el('span',t(label)),el('strong',value));if(note)node.append(el('small',t(note)));return node;}
 function render(){if(!report)return;const totals=report.summary,summary=$('usage-totals');summary.replaceChildren(metric('已记录 tokens',token(totals)),metric('任务数',number(totals.tasks)),metric('模型运行耗时',seconds(totals.model_seconds)),metric('缓存读取 tokens',number(totals.cache_read_tokens)));
  const chart=$('usage-chart');chart.replaceChildren();chart.hidden=!report.daily.length;const max=Math.max(1,...report.daily.map(day=>day.tokens));for(const day of report.daily){const column=el('div',undefined,'usage-column'),bar=el('progress');bar.max=max;bar.value=day.tokens;bar.setAttribute('aria-label',new Date(day.day*1000).toISOString().slice(0,10)+' '+token(day));column.title=bar.getAttribute('aria-label');column.append(bar,el('small',new Date(day.day*1000).toISOString().slice(5,10)));chart.append(column);}
  $('usage-cost').textContent=t('费用仅为有价格记录的调用估算，不代表账单。')+' '+(totals.estimated_usd==null?t('价格未知'):'USD '+totals.estimated_usd.toFixed(4))+(totals.unpriced_calls?' · '+number(totals.unpriced_calls)+' '+t('次调用未计入估算'):'');
  const list=$('usage-tasks');list.replaceChildren();for(const row of report.tasks){const article=el('article',undefined,'usage-task'),title=el('div');title.append(el('strong',row.background?t('后台检查与连接测试'):(row.title||row.task)),el('small',(state.projects.find(p=>p.id===row.project)?.name||t('通用'))+' · '+t(statusNames[row.state]||row.state||'后台检查')));const metrics=el('div',undefined,'usage-task-numbers');metrics.append(el('strong',token(row)),el('small',seconds(row.model_seconds)+' · '+row.attempts+' '+t('轮尝试')));article.append(title,metrics);if(row.task)article.append(action(t('查看用量明细'),()=>detail(row.task,row.title),'text-button'));else article.append(el('small',row.models||'','muted'));list.append(article);}if(!report.tasks.length)list.append(el('p',t('所选时间没有用量记录。'),'muted'));
  $('usage-page').textContent=number(report.total)+' '+t('条记录')+' · '+number(report.total?offset+1:0)+'–'+number(Math.min(offset+25,report.total));$('usage-prev').disabled=offset===0;$('usage-next').disabled=offset+25>=report.total;
 }
 async function detail(task,title,page=0){const ticket=++detailSequence,params=parameters();params.set('task',task);params.set('offset',page);params.delete('q');const data=await api('usage?'+params);if(ticket!==detailSequence||view!=='usage')return;const dialog=$('usage-detail'),content=$('usage-detail-content');content.replaceChildren(el('h2',title||task),el('p',t('所选时间内累计，包含失败调用与所有重试。'),'muted'));
  const roles=el('div',undefined,'usage-role-list');for(const row of data.roles){const line=el('div',undefined,'usage-task');line.append(el('span',t(roleNames[row.role]||row.role)),el('small',row.provider+' / '+row.model+' · '+row.effort),el('strong',token(row)));roles.append(line);}content.append(roles);
  for(const row of data.calls){const line=el('div',undefined,'usage-call');line.append(el('strong',t(roleNames[row.role]||row.role)+' · '+row.model),el('small',stamp(row.created)+' · '+row.provider+' · '+row.effort+' · '+t(row.state==='done'?'已完成':row.state==='reserved'?'等待返回用量':statusNames[row.state]||row.state)),el('p',t('输入')+' '+(row.usage_known?number(row.input_tokens):t('未知'))+' / '+t('输出')+' '+(row.usage_known?number(row.output_tokens):t('未知'))+' / '+t('缓存读取')+' '+(row.usage_known?number(row.cache_read_tokens):t('未知'))+' / '+t('缓存写入')+' '+(row.usage_known?number(row.cache_write_tokens):t('未知'))),el('small',(row.usage_known?number(row.tokens):t('用量未知'))+' tokens · '+seconds(row.seconds)+' · '+t(({http_request:'API 请求',codex_session:'Codex 会话',opencode_session:'OpenCode 会话',unknown:'计量单位未知'})[row.unit]||row.unit)));content.append(line);}
  const pages=el('div',undefined,'task-pagination');if(page)pages.append(action(t('上一页'),()=>detail(task,title,Math.max(0,page-25))));pages.append(el('span',number(data.call_total)+' '+t('次调用')));if(page+25<data.call_total)pages.append(action(t('下一页'),()=>detail(task,title,page+25)));content.append(pages);if(!dialog.open)dialog.showModal();
 }
 function show(){offset=0;safe(load);}
 document.addEventListener('DOMContentLoaded',()=>{for(const id of ['usage-period','usage-scope','usage-sort','usage-start','usage-end'])$(id).addEventListener('change',()=>{$('usage-custom').hidden=$('usage-period').value!=='custom';show();});$('usage-search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(show,350);});$('usage-refresh').onclick=()=>safe(load);$('usage-prev').onclick=()=>{offset=Math.max(0,offset-25);safe(load);};$('usage-next').onclick=()=>{offset+=25;safe(load);};$('project-select').addEventListener('change',()=>{if(view==='usage')show();});});
 return {show,translate:()=>{render();detailSequence++;if($('usage-detail').open)$('usage-detail').close();}};
})();
