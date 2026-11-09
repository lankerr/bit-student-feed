'use strict';
const $ = id => document.getElementById(id);
const labels={academic:'学术讲座',competition:'竞赛创新',career:'升学就业',volunteer:'实践志愿',campus_service:'校园服务',other:'其他'};
const states={actionable:'可参加',needs_verification:'待核验',expired:'已结束',recap:'活动回顾',not_event:'资讯通知'};
let articles=[], scope='all', shown=30;
const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function active(a){const c=a.classification;return c?.verified_activity && ['deadline','event_date'].every(k=>!c[k]||c[k]>=today);}
function link(title,url){const a=el('a',title);const u=new URL(url);if(!['https:','http:'].includes(u.protocol)||!(u.hostname==='mp.weixin.qq.com'||u.hostname==='bit.edu.cn'||u.hostname.endsWith('.bit.edu.cn')))throw new Error('无效来源');a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}
function render(){const q=$('query').value.trim().toLowerCase(),cat=$('category').value,sort=$('sort').value;
let list=articles.filter(a=>(scope==='all'||(scope==='active'?active(a):!a.classification||a.classification.event_status==='needs_verification'))&&(cat==='all'||a.classification?.category===cat)&&(!q||[a.title,a.source,a.classification?.summary].join(' ').toLowerCase().includes(q)));
list.sort((a,b)=>sort==='rank'?(b.classification?.score??-1)-(a.classification?.score??-1)||b.published_at-a.published_at:sort==='deadline'?((active(a)&&a.classification.deadline)||'9999').localeCompare((active(b)&&b.classification.deadline)||'9999')||b.published_at-a.published_at:b.published_at-a.published_at);
$('count').textContent=list.length+' 条';$('feed').replaceChildren();
for(const a of list.slice(0,shown)){const c=a.classification,n=el('article',undefined,'article'),date=el('div',a.date,'article-date');date.append(el('small','发布'));const body=el('div'),h=el('h3');h.append(link(a.title,a.url));body.append(h);
const meta=el('div',undefined,'meta');meta.append(el('span',a.source));meta.append(el('span',c?labels[c.category]:'待分拣','tag'+(!c?' pending':'')));let state=c?.event_status;if(state==='actionable'&&!active(a))state='expired';meta.append(el('span',states[state]||'等待学校模型分拣',(!c||state==='needs_verification')?'tag pending':state==='recap'?'tag recap':''));body.append(meta);
if(c?.summary)body.append(el('p',c.summary,'summary'));if(active(a)){body.append(el('p',[c.deadline?'报名截止 '+c.deadline:'',c.event_date?'活动日期 '+c.event_date:''].filter(Boolean).join(' · '),'next'));body.append(el('p','建议：'+c.next_step,'summary'));}n.append(date,body,el('span','↗','arrow'));$('feed').append(n);}
if(!list.length)$('feed').append(el('p',scope==='active'?'当前没有通过来源与日期核验的可参加活动。请查看全部消息或待核验事项。':'没有符合条件的消息。','empty'));
$('more').hidden=list.length<=shown;}
document.querySelectorAll('[data-scope]').forEach(b=>b.addEventListener('click',()=>{scope=b.dataset.scope;shown=30;document.querySelectorAll('[data-scope]').forEach(x=>x.classList.toggle('selected',x===b));render();}));
for(const id of ['query','category','sort'])$(id).addEventListener('input',()=>{shown=30;render();});$('more').addEventListener('click',()=>{shown+=30;render();});
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{const url=new URL(b.dataset.copy,location.href).href;try{await navigator.clipboard.writeText(url);$('copy-status').textContent='订阅地址已复制';}catch{$('copy-status').textContent='请打开 RSS 链接并复制浏览器地址。';}}));
(async()=>{try{const r=await fetch('data.json',{cache:'no-cache'});if(!r.ok)throw new Error('读取失败');const d=await r.json();if(d.schema_version!==2)throw new Error('格式不兼容');articles=d.articles;
const fresh=d.collection?.source_refreshed_at||d.collection?.last_collection;const age=fresh?(Date.now()-new Date(fresh).getTime())/3600000:Infinity;
$('freshness').textContent=(age>36?'来源同步需要检查 · ':'来源同步 ')+(fresh?new Date(fresh).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'时间未提供')+' · 页面生成 '+new Date(d.generated_at).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'});
$('coverage').textContent=d.coverage+' 发布时间不代表活动时间。';$('day').textContent=today+' · 今日指引';$('guidance').replaceChildren();const items=articles.filter(active).slice(0,3);
if(!items.length)$('guidance').append(el('p','当前没有通过日期与来源核验的可参加活动。先看看新通知；待分拣、已结束和活动回顾不会列入推荐。'));
for(const a of items){const n=el('div',undefined,'guide-item');n.append(link(a.title,a.url),el('p',a.classification.next_step));$('guidance').append(n);}render();
}catch(e){$('freshness').textContent='更新数据暂时无法读取';$('guidance').textContent='请稍后刷新页面。';$('feed').replaceChildren(el('p','数据加载失败，请稍后重试或到 GitHub 查看运行记录。','empty'));}})();
