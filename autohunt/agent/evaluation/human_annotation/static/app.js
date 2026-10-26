const $ = id => document.getElementById(id);
const state = {token:'',items:[],task:null,review:null,dirty:false,busy:false,timer:null,request:0,queueRequest:0,blocked:false};
const labels = {unlabeled:'未标注',draft:'草稿',submitted:'已完成',skipped:'已跳过'};
const eligibility = {pass:'满足',fail:'不满足',unknown:'信息不足'};
let prefs={};try{prefs=JSON.parse(localStorage.getItem('autohunt-annotation-prefs')||'{}')||{};}catch{/* Invalid preferences never block the workbench. */}
$('reviewer').value = prefs.reviewer || '标注员 A';
$('scope').value = ['calibration','development','test','all'].includes(prefs.scope) ? prefs.scope : 'calibration';
$('slot').value = ['a','b','adjudicated'].includes(prefs.slot) ? prefs.slot : 'a';
let activeContext = context();
function context(){return {scope:$('scope').value,slot:$('slot').value,reviewer:$('reviewer').value.trim()};}
function resumeKey(){return `${activeContext.scope}:${activeContext.slot}:${activeContext.reviewer}`;}
function preferences(){
  prefs={...context(),resume:prefs.resume||{}};
  if(state.task)prefs.resume[resumeKey()]=state.task.id;
  try{localStorage.setItem('autohunt-annotation-prefs',JSON.stringify(prefs));}catch{/* Labels are durable in SQLite, even with browser storage disabled. */}
}
function error(message){$('error-text').textContent=message;$('error').hidden=false;}
function clearError(){$('error').hidden=true;}
async function api(path, options={}){
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-Annotation-Token':state.token,...options.headers}});
  const value=await response.json();
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'请求格式不正确，请检查输入后重试。');
  return value;
}
function element(tag, text, cls){const node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;}
function snapshot(){return JSON.stringify(state.review);}
function updateStatus(text){$('save-status').textContent=text;}
function changed(){
  if(!state.review||state.busy||state.blocked)return;
  state.review.notes=$('notes').value;
  state.review.flagged=$('flagged').checked;
  state.dirty=true;updateStatus('未保存');
  clearTimeout(state.timer);state.timer=setTimeout(()=>save('draft').catch(e=>error(e.message)),900);
}
function defaultReview(){return {relevance:null,eligibility:null,evidence:[],notes:'',flagged:false,status:'draft',revision:0};}
function selectEvidence(source,line_id){
  if(!state.review||state.busy||state.blocked)return;
  const index=state.review.evidence.findIndex(e=>e.source===source&&e.line_id===line_id);
  if(index>=0)state.review.evidence.splice(index,1);else state.review.evidence.push({source,line_id});
  renderSelections();changed();
}
function renderDocument(source){
  const target=$(source==='candidate'?'candidate-lines':'job-lines');target.replaceChildren();
  for(const [line_id,text] of Object.entries(state.task[source].lines)){
    if(line_id==='c0'||line_id==='j0'){target.append(element('p',text,'disclaimer'));continue;}
    const line=element('button',text,'doc-line');line.dataset.line=line_id;line.dataset.source=source;
    line.setAttribute('aria-pressed','false');line.title='点击选为证据';line.addEventListener('click',()=>selectEvidence(source,line_id));target.append(line);
  }
}
function renderSelections(){
  for(const button of document.querySelectorAll('[data-relevance]'))button.setAttribute('aria-pressed',String(state.review?.relevance===Number(button.dataset.relevance)));
  for(const button of document.querySelectorAll('[data-eligibility]'))button.setAttribute('aria-pressed',String(state.review?.eligibility===button.dataset.eligibility));
  for(const button of document.querySelectorAll('.doc-line')){
    const selected=state.review?.evidence.some(e=>e.source===button.dataset.source&&e.line_id===button.dataset.line);
    button.classList.toggle('selected',!!selected);button.setAttribute('aria-pressed',String(!!selected));
  }
  $('evidence-count').textContent=`${state.review?.evidence.length||0} 段`;
  $('evidence-list').replaceChildren();
  if(!state.review?.evidence.length){$('evidence-list').append(element('p','从左侧材料和 JD 各选择至少一段。缺少证据的结论会标记为需复核。','muted'));return;}
  for(const evidence of state.review.evidence){
    const row=element('div',null,'evidence-chip');const body=element('div');
    body.append(element('small',`${evidence.source==='candidate'?'候选人':'JD'} · ${evidence.line_id}`));
    body.append(element('p',state.task[evidence.source].lines[evidence.line_id]));
    const remove=element('button','×');remove.setAttribute('aria-label',`移除证据 ${evidence.line_id}`);remove.addEventListener('click',()=>selectEvidence(evidence.source,evidence.line_id));
    row.append(body,remove);$('evidence-list').append(row);
  }
}
function renderQueue(counts){
  $('queue-list').replaceChildren();
  for(const item of state.items){
    const button=element('button',null,`queue-item ${item.status}${item.flagged?' flagged':''}${state.task?.id===item.id?' active':''}`);
    button.dataset.id=item.id;button.setAttribute('aria-current',state.task?.id===item.id?'true':'false');
    const body=element('span');body.append(element('strong',item.role),element('small',`${item.id.slice(-8)} · ${labels[item.status]}${item.flagged?' · 需复核':''}`));
    button.append(element('span',String(item.number).padStart(3,'0'),'queue-number'),body,element('span',null,'status-dot'));
    button.addEventListener('click',()=>openTask(item.id));$('queue-list').append(button);
  }
  $('queue-count').textContent=String(state.items.length);
  if(counts){
    $('progress-text').textContent=`已完成 ${counts.submitted} / ${counts.total}`;
    $('progress-percent').textContent=`${Math.round(counts.submitted/Math.max(1,counts.total)*100)}%`;
    $('progress').max=counts.total||1;$('progress').value=counts.submitted;
  }
  const index=state.items.findIndex(i=>i.id===state.task?.id);
  $('position').textContent=index>=0?`${index+1} / ${state.items.length}`:`0 / ${state.items.length}`;
  $('previous').disabled=index<=0;$('next').disabled=index<0||index>=state.items.length-1;
}
async function queue(keep=true){
  const generation=++state.queueRequest;
  const params=new URLSearchParams({scope:activeContext.scope,slot:activeContext.slot,state:$('state').value,q:$('search').value});
  const result=await api(`/api/tasks?${params}`);if(generation!==state.queueRequest)return;state.items=result.items;renderQueue(result.counts);
  if(!keep||!state.items.some(i=>i.id===state.task?.id)){
    const resume=prefs.resume?.[resumeKey()];
    await openTask(state.items.find(i=>i.id===resume)?.id||state.items[0]?.id,null,true);
  }
}
async function openTask(id,unused=null,alreadySaved=false){
  if(state.busy)return;
  try{
    if(!alreadySaved&&state.dirty)await save('draft');
    clearTimeout(state.timer);clearError();
    if(!id){state.task=null;state.review=null;$('documents').hidden=true;$('empty').hidden=false;setDisabled(true);renderQueue();return;}
    const generation=++state.request;
    const task=await api(`/api/tasks/${encodeURIComponent(id)}?${new URLSearchParams({slot:activeContext.slot,reviewer:activeContext.reviewer})}`);
    if(generation!==state.request)return;
    state.task=task;state.review=task.review?{...task.review}:defaultReview();state.dirty=false;state.blocked=task.occupied;
    $('documents').hidden=false;$('empty').hidden=true;$('candidate-role').textContent=task.candidate.role;$('job-title').textContent=task.job.title;
    const item=state.items.find(i=>i.id===id);$('sample-number').textContent=`样本 ${String(item?.number||'—').padStart(3,'0')}`;
    $('sample-split').textContent=task.split==='test'?'保留测试集':'开发集';$('pair-id').textContent=task.id;
    $('notes').value=state.review.notes;$('flagged').checked=state.review.flagged;
    $('reference-results').hidden=true;$('reference-results').replaceChildren();
    $('reference-warning').textContent=state.review.reference_exposed?'此标注员已查看过该样本的他人标注，记录会保留。':'默认不展示他人或模型答案。查看会记录为非盲审。';
    renderDocument('candidate');renderDocument('job');renderSelections();setDisabled(task.occupied);renderQueue();
    updateStatus(task.occupied?'已有他人记录':labels[state.review.status]||'未标注');
    if(task.occupied)error('该轮次已由另一标注员保存。已隐藏其答案；请使用空的轮次，或填回原标注员名称。');
    document.querySelector('.queue-item.active')?.scrollIntoView({block:'nearest'});
    $('queue-panel').classList.remove('open');preferences();
  }catch(e){error(e.message);}
}
function setDisabled(disabled){for(const node of document.querySelectorAll('.judgement button,.judgement textarea,.judgement input'))node.disabled=disabled;}
async function save(status='draft'){
  clearTimeout(state.timer);
  if(state.busy)throw new Error('正在保存，请稍候再操作。');
  if(!state.task||state.blocked)return;
  if(!activeContext.reviewer)throw new Error('请先填写标注员名称。');
  state.busy=true;setDisabled(true);updateStatus('正在保存…');
  const original=snapshot();
  try{
    const payload={slot:activeContext.slot,reviewer:activeContext.reviewer,expected_revision:state.review.revision||0,
      relevance:state.review.relevance,eligibility:state.review.eligibility,evidence:state.review.evidence.map(({source,line_id})=>({source,line_id})),
      notes:$('notes').value,status,flagged:$('flagged').checked};
    const saved=await api(`/api/reviews/${state.task.id}`,{method:'PUT',body:JSON.stringify(payload)});
    if(snapshot()===original){state.review=saved;state.dirty=false;$('flagged').checked=saved.flagged;}
    updateStatus(saved.flagged?'已保存 · 需复核':status==='submitted'?'已完成 · 本机保存':'草稿已保存');
    const item=state.items.find(i=>i.id===state.task.id);if(item){item.status=saved.status;item.flagged=saved.flagged;}
    if(saved.warnings.length)error(`已保存，需复核：${saved.warnings.join('；')}。`);
    return saved;
  }catch(e){state.dirty=true;updateStatus('保存失败 · 修改保留');throw e;}
  finally{state.busy=false;setDisabled(state.blocked);}
}
async function finish(status){
  const current=state.task?.id;const index=state.items.findIndex(i=>i.id===current);const next=state.items[index+1]?.id;
  try{clearError();await save(status);await queue(true);await openTask(next||state.items.find(i=>i.status!=='submitted'&&i.id!==current)?.id||current,null,true);}
  catch(e){error(e.message);}
}
for(const button of document.querySelectorAll('[data-relevance]'))button.addEventListener('click',()=>{state.review.relevance=Number(button.dataset.relevance);renderSelections();changed();});
for(const button of document.querySelectorAll('[data-eligibility]'))button.addEventListener('click',()=>{state.review.eligibility=button.dataset.eligibility;renderSelections();changed();});
$('notes').addEventListener('input',changed);$('flagged').addEventListener('change',changed);
$('save').addEventListener('click',async()=>{try{clearError();await save('draft');await queue(true);}catch(e){error(e.message);}});
$('complete').addEventListener('click',()=>finish('submitted'));$('skip').addEventListener('click',()=>finish('skipped'));
for(const [id,delta] of [['previous',-1],['next',1]])$(id).addEventListener('click',()=>openTask(state.items[state.items.findIndex(i=>i.id===state.task?.id)+delta]?.id));
async function switchContext(){
  const next=context();
  try{if(state.dirty)await save('draft');activeContext=next;preferences();await queue(false);}
  catch(e){for(const key of ['slot','scope','reviewer'])$(key).value=activeContext[key];error(e.message);}
}
for(const id of ['scope','slot','reviewer'])$(id).addEventListener('change',switchContext);
$('state').addEventListener('change',async()=>{try{if(state.dirty)await save('draft');await queue(true);}catch(e){error(e.message);}});
let searchTimer;$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(async()=>{try{if(state.dirty)await save('draft');await queue(true);}catch(e){error(e.message);}},300);});
$('compare').addEventListener('click',async()=>{
  try{
    if(state.dirty)await save('draft');
    const result=await api(`/api/compare/${state.task.id}`,{method:'POST',body:JSON.stringify({reviewer:activeContext.reviewer})});
    $('reference-warning').textContent=result.notice;$('reference-results').replaceChildren();$('reference-results').hidden=false;
    for(const [slot,title] of [['a','标注 A'],['b','标注 B'],['adjudicated','仲裁']]){
      const r=result[slot],row=element('div',null,'reference-row');row.append(element('strong',title));
      if(!r)row.append(element('p','尚未标注'));else {row.append(element('p',`${r.reviewer} · ${r.relevance??'未选'}级 · ${eligibility[r.eligibility]||'未选硬条件'} · ${labels[r.status]}`));row.append(element('p',r.notes||'未填写理由'));}
      $('reference-results').append(row);
    }
  }catch(e){error(e.message);}
});
$('queue-toggle').addEventListener('click',()=>$('queue-panel').classList.toggle('open'));
$('error-close').addEventListener('click',clearError);
$('help-open').addEventListener('click',()=>$('help-dialog').showModal());
$('export-open').addEventListener('click',async()=>{try{if(state.dirty)await save('draft');$('export-dialog').showModal();}catch(e){error(e.message);}});
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).close());
document.addEventListener('keydown',event=>{
  if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&state.task&&!state.busy&&!state.blocked){event.preventDefault();finish('submitted');return;}
  if(event.target.closest('input,textarea,select,dialog')||document.querySelector('dialog[open]')||event.ctrlKey||event.metaKey||event.altKey||!state.review||state.busy||state.blocked)return;
  if(['1','2','3'].includes(event.key)){event.preventDefault();document.querySelector(`[data-relevance="${Number(event.key)-1}"]`).click();}
  const map={q:'pass',w:'fail',e:'unknown'};if(map[event.key.toLowerCase()]){event.preventDefault();document.querySelector(`[data-eligibility="${map[event.key.toLowerCase()]}"]`).click();}
});
window.addEventListener('beforeunload',event=>{if(state.dirty||state.busy){event.preventDefault();event.returnValue='';}});
async function boot(){try{const config=await api('/api/status');state.token=config.token;$('dataset-count').textContent=`${config.pairs} 对`;await queue(false);}catch(e){error(`无法读取本地标注服务：${e.message}`);}}
boot();
