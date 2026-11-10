'use strict';
const $ = id => document.getElementById(id);
const labels={academic:'学术讲座',competition:'竞赛创新',career:'升学就业',volunteer:'实践志愿',campus_service:'校园服务',other:'其他'};
const states={actionable:'可参加',needs_verification:'待核验',expired:'已结束',recap:'活动回顾',not_event:'资讯通知'};
const sourceStates={ok:'正常',partial:'部分完成',failed:'采集失败',empty:'未取到消息',not_configured:'未配置',unconfigured:'未配置',pending_configuration:'未配置',disabled:'未启用',pending:'待检查',rate_limited:'频控冷却',auth_expired:'授权已过期',auth_missing:'未授权',blocked:'验证受阻',pending_follow:'待关注',follow_failed:'关注失败'};
const unconfigured=s=>['not_configured','unconfigured','pending_configuration'].includes(s.state)||s.configured===false;
let articles=[], scope='all', shown=30;
const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());

function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function shanghaiDay(seconds){if(!seconds)return '';const d=new Date(seconds*1000);return Number.isNaN(d.getTime())?'':new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);}
function dueDays(a){const c=a.classification||{};return [c.deadline,c.event_date,shanghaiDay(a.source_expiry)].filter(Boolean);}
function active(a){const c=a.classification;return Boolean(c?.verified_activity&&c.event_status==='actionable'&&dueDays(a).every(day=>day>=today));}
function due(a){const days=dueDays(a);return days.length?days.slice().sort()[0]:'9999-12-31';}
function chineseDay(day){return day&&day.length>=10?day.slice(0,4)+'年'+Number(day.slice(5,7))+'月'+Number(day.slice(8,10))+'日':'';}
function plusDays(iso,count){const [y,m,d]=iso.split('-').map(Number);return new Date(Date.UTC(y,m-1,d+count)).toISOString().slice(0,10);}
function articleState(a){const c=a.classification;if(!c)return null;if(c.event_status==='actionable'&&!active(a))return dueDays(a).some(day=>day<today)?'expired':'needs_verification';return c.event_status;}
function dateTime(value){if(!value)return '暂无成功记录';const d=new Date(value);return Number.isNaN(d.getTime())?'时间未注明':d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});}
function countText(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0?String(value):'—';}
function link(title,url){const a=el('a',title);const u=new URL(url);if(!['https:','http:'].includes(u.protocol)||u.username||u.password||!(u.hostname==='mp.weixin.qq.com'||u.hostname==='bit.edu.cn'||u.hostname.endsWith('.bit.edu.cn')))throw new Error('无效来源');a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}

function sourceError(value){const raw=String(value||'');if(/^HTTP [1-5]\d\d$/.test(raw))return raw;const errors={Timeout:'请求超时',ReadTimeout:'读取超时',ConnectTimeout:'连接超时',ConnectionError:'连接失败',JSONDecodeError:'返回格式异常',ValueError:'列表或分页校验未通过'};return errors[raw]||(raw?'请求或解析失败':'');}
function sourceBoundary(s){
  if(unconfigured(s))return '尚未接入，不计入已覆盖范围';
  if(s.state==='disabled')return s.disabled_reason==='upstream_list_unavailable'?'文章列表未接通，自动请求已暂停；上次返回 200013':'当前未启用';
  if(s.state==='rate_limited')return '上游返回 200013；冷却不保证恢复'+(s.retry_after?'。禁止早于 '+dateTime(s.retry_after*1000)+' 重试':'');
  if(s.state==='auth_expired'||s.state==='auth_missing')return '需要服务器端扫码授权';
  if(s.error||s.state==='failed')return '本轮未完成，历史收录继续保留';
  if(s.truncated)return s.pagination_status==='cap_reached'?'已到分页上限，仍有未查页':'本轮分页未完成';
  if(s.scope==='weread_latest')return '经微信读书订阅，每次只看到最新一篇；多图文的第二篇及以后可能遗漏';
  if(s.scope==='observed_list_pages')return s.complete?'已检查可见分页，不代表全历史':'仅检查已开放的列表范围';
  if(s.complete&&typeof s.count==='number'&&s.fetched===s.count)return '本轮数量与源端报告一致';
  if(s.complete)return '本轮分页检查完成，范围以来源为准';
  if(s.state==='empty')return '本轮未取到记录，需核对来源';
  return '覆盖范围尚待核对';
}
function renderSources(data){
  const sources=Array.isArray(data.sources)?data.sources:[];
  $('source-rows').replaceChildren();
  if(!sources.length){const row=el('tr'),cell=el('td','尚未提供来源核对清单，当前无法确认覆盖范围。');cell.colSpan=6;row.append(cell);$('source-rows').append(row);$('source-summary').textContent='来源范围待核对';return sources;}
  const missing=sources.filter(unconfigured).length;
  const attention=sources.filter(s=>!unconfigured(s)&&(s.error||s.truncated||['failed','partial','empty','rate_limited','auth_expired','auth_missing','blocked','follow_failed'].includes(s.state))).length;
  $('source-summary').textContent='已配置 '+(sources.length-missing)+' 项 · 待处理 '+attention+' 项 · 未配置 '+missing+' 项';
  for(const s of sources){
    const row=el('tr'),name=el('th',s.name||s.source||s.id||'未命名来源');name.scope='row';
    const status=el('td'),state=unconfigured(s)?'未配置':sourceStates[s.state]||'状态未注明';
    status.append(el('span',state,'source-state '+(unconfigured(s)||s.state==='disabled'?'source-off':s.error||['partial','failed','empty','rate_limited','auth_expired','auth_missing','blocked'].includes(s.state)?'source-attention':'source-ok')));
    const error=sourceError(s.error);if(error)status.append(el('small',error,'source-error'));
    row.append(name,status,el('td',unconfigured(s)?'—':countText(s.pages)+' 页'),el('td',unconfigured(s)?'—':countText(s.fetched)+' / '+countText(s.count)),el('td',unconfigured(s)?'—':dateTime(s.last_successful_at)),el('td',sourceBoundary(s),'source-scope'));
    $('source-rows').append(row);
  }
  return sources;
}

function bodyNote(a){
  const statuses={available:'已获取正文，安排仍需核对原文',fetched:'已获取正文，安排仍需核对原文',full:'已获取正文，安排仍需核对原文',full_text:'已获取正文，安排仍需核对原文',structured_list_metadata:'依据官网列表字段，未核对全文',title_and_publication_date:'仅标题与列表日期，未核对全文',not_fetched:'未获取正文',metadata_only:'仅消息索引，未核对全文',missing:'正文尚未获取'};
  const body=statuses[a.body_status]||'正文获取状态未注明，需核对原文';
  return (a.classification?'模型：'+(a.model||'版本未注明')+' · ':'等待模型分拣 · ')+body;
}
function sourceEventNote(a){
  if(a.source_event_start||a.source_event_end)return '官网活动时间：'+[a.source_event_start,a.source_event_end].filter(Boolean).join(' 至 ');
  if(a.source_application_deadline)return '官网招聘有效截止：'+a.source_application_deadline;
  return '';
}
function render(){
  const q=$('query').value.trim().toLowerCase(),cat=$('category').value,sort=$('sort').value;
  let list=articles.filter(a=>(scope==='all'||(scope==='active'?active(a):!a.classification||articleState(a)==='needs_verification'))&&(cat==='all'||a.classification?.category===cat)&&(!q||[a.title,a.source,a.classification?.summary].join(' ').toLowerCase().includes(q)));
  const newest=(a,b)=>Number(a.publication_status==='unknown')-Number(b.publication_status==='unknown')||b.published_at-a.published_at;
  const past=a=>['expired','recap'].includes(articleState(a));
  list.sort((a,b)=>sort==='rank'?Number(past(a))-Number(past(b))||(b.classification?.score??-1)-(a.classification?.score??-1)||newest(a,b):sort==='deadline'?((active(a)&&(a.classification.deadline||a.classification.event_date))||'9999').localeCompare((active(b)&&(b.classification.deadline||b.classification.event_date))||'9999')||newest(a,b):newest(a,b));
  $('count').textContent=list.length+' 条';$('feed').replaceChildren();
  for(const a of list.slice(0,shown)){
    const c=a.classification,n=el('article',undefined,'article'),date=el('div',a.date||'日期未注明','article-date');
    date.append(el('small',a.publication_status==='unknown'?'首次收录':'发布'));const body=el('div'),h=el('h3');h.append(link(a.title,a.url));body.append(h);
    const meta=el('div',undefined,'meta');meta.append(el('span',a.source));
    if(String(a.origin||'').includes('server'))meta.append(el('span','服务端采集'));
    meta.append(el('span',c?labels[c.category]||'其他':'待分拣','tag'+(!c?' pending':'')));
    const state=articleState(a);meta.append(el('span',states[state]||'等待模型分拣',(!c||state==='needs_verification')?'tag pending':state==='recap'?'tag recap':''));body.append(meta);
    if(c?.summary)body.append(el('p',c.summary,'summary'));
    const event=sourceEventNote(a);if(event)body.append(el('p',event,'event-source'));
    if(active(a)){body.append(el('p',[c.deadline?'报名截止 '+c.deadline:'',c.event_date?'活动日期 '+c.event_date:''].filter(Boolean).join(' · '),'next'));body.append(el('p','建议：'+c.next_step,'summary'));}
    body.append(el('p',bodyNote(a),'verification-note'));n.append(date,body,el('span','↗','arrow'));$('feed').append(n);
  }
  if(!list.length)$('feed').append(el('p',scope==='active'?'当前没有通过来源与日期核验的可参加活动。请查看全部消息或待核验事项。':'没有符合条件的消息。','empty'));
  $('more').hidden=list.length<=shown;
}

document.querySelectorAll('[data-scope]').forEach(b=>b.addEventListener('click',()=>{scope=b.dataset.scope;shown=30;document.querySelectorAll('[data-scope]').forEach(x=>x.classList.toggle('selected',x===b));render();}));
for(const id of ['query','category','sort'])$(id).addEventListener('input',()=>{shown=30;render();});
$('more').addEventListener('click',()=>{shown+=30;render();});
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{const url=new URL(b.dataset.copy,location.href).href;try{await navigator.clipboard.writeText(url);$('copy-status').textContent='订阅地址已复制';}catch{$('copy-status').textContent='请打开 RSS 链接并复制浏览器地址。';}}));

(async()=>{
  try{
    const r=await fetch('data.json',{cache:'no-cache'});if(!r.ok)throw new Error('读取失败');const d=await r.json();if(d.schema_version!==2||!Array.isArray(d.articles))throw new Error('格式不兼容');articles=d.articles;
    const sources=renderSources(d),successes=sources.map(s=>s.last_successful_at).filter(v=>v&&!Number.isNaN(new Date(v).getTime())).sort((a,b)=>new Date(b)-new Date(a));
    const fresh=successes[0]||d.collection?.source_refreshed_at,age=fresh?(Date.now()-new Date(fresh).getTime())/3600000:Infinity;
    $('freshness').textContent=(fresh?(age>36?'采集更新需要检查 · 最近成功采集 ':'最近成功采集 ')+dateTime(fresh):'尚无成功采集时间记录')+' · 页面生成 '+dateTime(d.generated_at);
    $('coverage').textContent='只展示服务端已收录的公开消息，覆盖范围见来源清单。首次收录、发布日期和活动日期分别标注；模型分拣不等于全文核验。';
    $('day').textContent=today+' · 今日指引';$('guidance').replaceChildren();
    const ranked=articles.filter(active).sort((a,b)=>due(a).localeCompare(due(b))||a.title.localeCompare(b.title,'zh'));
    const near=ranked.filter(a=>due(a)<=plusDays(today,14));
    const items=(near.length?near:ranked).slice(0,3);
    if(!items.length)$('guidance').append(el('p','当前没有通过日期与来源核验的可参加活动。先看看新通知；待分拣、已结束和活动回顾不会列入推荐。'));
    else if(!near.length)$('guidance').append(el('p','未来 14 天没有已核验的活动。下面是最近仍开放的安排。'));
    for(const a of items){const n=el('div',undefined,'guide-item');n.append(link(a.title,a.url),el('p',chineseDay(due(a))+' · '+a.classification.next_step));$('guidance').append(n);}
    render();
  }catch(e){
    $('freshness').textContent='更新数据暂时无法读取';$('guidance').textContent='请稍后刷新页面。';$('source-summary').textContent='来源记录暂时无法读取';
    const row=el('tr'),cell=el('td','来源记录读取失败，当前无法确认覆盖范围。');cell.colSpan=6;row.append(cell);$('source-rows').replaceChildren(row);
    $('feed').replaceChildren(el('p','数据加载失败，请稍后重试或到 GitHub 查看运行记录。','empty'));
  }
})();
