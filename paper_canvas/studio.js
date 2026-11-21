"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="studio-token"]').content;
const live = document.querySelector('meta[name="live"]').content === "true";
let state, activeTab = "poster", working = false;
const labels = {question:"研究问题",method:"方法",result:"主要结果",figure:"证据图"};
const keys = Object.keys(labels);
function node(tag,text,cls) {
  const out=document.createElement(tag);
  if(text !== undefined) out.textContent=text;
  if(cls) out.className=cls;
  return out;
}
function button(title,fn,cls="item") {
  const out=node("button",title,cls);
  out.onclick=fn;
  return out;
}
function artifact(session,version,file="preview.png") {
  return "/artifact/"+session+"/"+version+"/"+file;
}
async function api(path,data) {
  const res=await fetch("/"+path,{method:"POST",headers:{"Content-Type":"application/json","X-Canvas-Token":token},body:JSON.stringify(data)});
  const result=await res.json();
  if(!res.ok) throw Error(result.error || "请求失败");
  return result;
}
async function work(message,fn) {
  if(working) return;
  working=true;
  ["new","apply","generate","compareRun"].forEach(id=>$(id).disabled=true);
  $("status").textContent=message;
  $("importError").textContent="";
  try { await fn(); $("status").textContent=""; }
  catch(error) {
    $("status").textContent=error.message;
    if($("input").open) $("importError").textContent=error.message;
  } finally {
    working=false;
    ["new","generate"].forEach(id=>$(id).disabled=false);
    ["apply","compareRun"].forEach(id=>$(id).disabled=!state);
  }
}
function tab(name) {
  activeTab=name;
  document.querySelectorAll(".view").forEach(v=>v.hidden=v.id!==name);
  document.querySelectorAll("[data-tab]").forEach(b=>{
    if(b.dataset.tab===name) b.setAttribute("aria-current","page");
    else b.removeAttribute("aria-current");
  });
  if(name==="compare") $("comparePrompt").textContent="当前请求 · "+requestText();
}
function requestText() {
  const op=$("op").value, target=$("target").value;
  if(op==="enlarge") return "enlarge figure by "+$("gain").value+"%"+($("lock").checked?" and protect result":"");
  if(op==="shorten") return "shorten "+target+" to "+$("words").value+" words";
  if(op==="protect") return "protect "+target;
  return $("request").value.trim();
}
function form() {
  const op=$("op").value;
  $("targetWrap").hidden=!["shorten","protect"].includes(op);
  $("gainWrap").hidden=$("lockWrap").hidden=op!=="enlarge";
  $("wordsWrap").hidden=op!=="shorten";
  $("customWrap").hidden=op!=="custom";
  $("gainText").textContent=$("gain").value+"%";
}
function outcome(record) {
  if(record.accepted) return "已应用";
  if(record.operation==="clarify") return "需要澄清";
  if(record.reason.includes("protected text")) return "与文字锁定冲突";
  return "已保留原稿";
}
function issueText(issue) {
  if(typeof issue!=="string") return [issue.target,issue.problem,issue.change].filter(Boolean).join(" · ");
  const names={"figure wider than allocated result column":"证据图宽度超出结果栏","figure overlaps caption":"证据图与图注重叠","caption overlaps figure":"图注与证据图重叠"};
  return names[issue] || issue;
}
function decision(record) {
  if(record.accepted) return "几何与内容检查通过。";
  if(record.reason.includes("protected text")) return "目标词数要求改写已锁定的文字，已保留上一个版本。";
  if(record.operation==="clarify") return "请指定段落和目标词数，或设置证据图的放大比例。";
  return record.reason;
}
function widthOf(s) { return s.elements.find(e=>e.id==="figure").w; }
function metric(value,label) {
  const box=node("span");
  box.append(node("strong",value),document.createTextNode(label));
  return box;
}
function feedback(previous) {
  const last=state.history.at(-1);
  $("feedback").replaceChildren();
  if(!last) return;
  const parts=[node("strong",outcome(last),last.accepted?"success":"failure"),node("p",decision(last))];
  if(previous && last.accepted) {
    const changes=[];
    const before=widthOf(previous),after=widthOf(state);
    if(Math.abs(after-before)>.1) changes.push("图宽 "+before.toFixed(1)+" → "+after.toFixed(1)+" pt");
    state.sections.forEach((s,i)=>{
      if(s.text!==previous.sections[i].text) changes.push(labels[keys[i]]+" "+previous.sections[i].text.split(/\s+/).length+" → "+s.text.split(/\s+/).length+" 词");
    });
    if(changes.length) parts.push(node("p",changes.join(" · ")));
  }
  $("feedback").append(...parts);
}
function display(next,previous) {
  state=next;
  sessionStorage.setItem("studioSession",state.session);
  $("title").textContent=state.title;
  $("preview").src=artifact(state.session,state.artifact);
  $("download").href=artifact(state.session,state.artifact,"poster.pptx");
  $("download").hidden=$("preview").hidden=false;
  $("metrics").replaceChildren(metric(state.size.map(Math.round).join(" × "),"pt"),metric(widthOf(state).toFixed(1),"pt 图宽"),metric(state.history.filter(h=>h.accepted).length,"次已接受修改"));
  $("locks").textContent=state.protected.length?"文字锁定 · "+state.protected.map(k=>labels[k]).join("、"):"";
  $("mode").textContent=state.editorial?"源文校对提纲 · 本地排版":live?"视觉模型在线模式":state.planner==="fixture"?"本地运行 · 预设语义样例":"本地运行 · 原文抽取";
  feedback(previous);
  evidence(3);
  history();
  $("branches").replaceChildren();
  $("comparePrompt").textContent="当前请求 · "+requestText();
}
function evidence(index) {
  const items=[...state.sections,state.evidence,...(state.additional||[])], entry=items[index];
  $("evidenceList").replaceChildren(...items.map((e,i)=>{
    const b=button(labels[keys[i]] || e.label,()=>evidence(i),"item"+(index===i?" active":""));
    b.append(node("small","p. "+e.page));
    return b;
  }));
  $("sourcePage").src="/source/"+state.session+"/"+entry.page;
  $("pageLabel").textContent="第 "+entry.page+" 页";
  const isFig=Boolean(entry.box);
  $("crop").hidden=!isFig;
  if(isFig){
    const [l,t,r,b]=entry.box;
    Object.assign($("crop").style,{left:l*100+"%",top:t*100+"%",width:(r-l)*100+"%",height:(b-t)*100+"%"});
  }
  $("citation").replaceChildren(node("h3",isFig?"对应图注":"海报文字"),node("blockquote",isFig?entry.caption:entry.text));
  if(entry.required) $("citation").append(node("h3","原文依据"),node("blockquote",[entry.quote,entry.additional_quote].filter(Boolean).join(" ")),node("small","修订时保留"),node("p",entry.required.join(" · ")));
}
function history() {
  const steps=[{name:"初稿检查",version:"00-initial",detail:"核对初始文字与图表位置。"}];
  let accepted="00-initial";
  for(const e of state.trace){
    if(e.event==="pass" || e.event==="attempt") steps.push({
      name:e.event==="pass"?(e.role==="content"?"内容修订":"空间调整"):("编辑尝试 "+e.attempt),
      version:e.artifact,detail:[e.request,...e.issues.map(issueText)].filter(Boolean).join(" · ") || "检查通过。"
    });
    if(e.event==="pass" || (e.event==="edit" && e.accepted)) accepted=steps.at(-1).version;
    if(e.event==="edit") steps.push({name:outcome(e),version:accepted,detail:e.request+" · "+decision(e)});
  }
  function show(i){
    const s=steps[i];
    $("oldPreview").src=artifact(state.session,s.version);
    $("versionLabel").textContent=(i+1)+" / "+steps.length;
    $("historyTitle").textContent=s.name;
    $("historyReason").textContent=s.detail;
    [...$("timeline").children].forEach((b,k)=>b.classList.toggle("active",i===k));
  }
  $("timeline").replaceChildren(...steps.map((s,i)=>button(s.name,()=>show(i))));
  show(steps.length-1);
}
function showBranches(rows) {
  $("branches").replaceChildren(...rows.map(r=>{
    const box=node("section",undefined,"branch");
    const img=node("img"); img.src=artifact(r.session,r.version);img.alt="该策略的最后候选版本";
    box.append(node("h3",r.condition==="joint"?"内容与布局联合修订":"固定布局"),node("strong",outcome(r.decision),r.decision.accepted?"success":"failure"),
      img,node("p","候选图宽 "+r.width.toFixed(1)+" pt · "+r.attempts.length+" 次渲染检查"));
    r.attempts.forEach(a=>box.append(node("p","第 "+a.attempt+" 次 · "+(a.issues.map(issueText).join("；") || "通过"))));
    if(!r.attempts.length) box.append(node("p",decision(r.decision)));
    return box;
  }));
}
async function generate(){
  const data={sample:$("sample").value,width:Number($("width").value),height:Number($("height").value)};
  if(data.sample==="upload"){
    const file=$("file").files[0];
    if(!file || file.size>16e6) throw Error("请选择 16 MB 以内的 PDF。");
    data.sample="field";
    data.pdf=await new Promise((resolve,reject)=>{const f=new FileReader();f.onload=()=>resolve(f.result.split(",")[1]);f.onerror=reject;f.readAsDataURL(file);});
  }
  display(await api("generate",data));
  if($("input").open) $("input").close();
  tab("poster");
}
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>{if(state)tab(b.dataset.tab);});
$("new").onclick=()=>$("input").showModal();
$("close").onclick=()=>$("input").close();
$("sample").onchange=()=>{
  $("upload").hidden=$("sample").value!=="upload";
  const conference=["published","forest"].includes($("sample").value);
  $("width").value=conference?1120:1008;
  $("height").value=conference?760:540;
};
$("op").onchange=$("gain").oninput=form;
$("paperform").onsubmit=e=>{e.preventDefault();work("正在解析论文、排版并检查候选海报…",generate);};
$("apply").onclick=()=>work("正在检查编辑约束并渲染候选版本…",async()=>{
  const before=state;
  display(await api("edit",{session:state.session,request:requestText()}),before);
});
$("compareRun").onclick=()=>work("正在从相同初稿独立运行两种策略…",async()=>{
  showBranches(await api("compare",{session:state.session,request:requestText()}));
});
$("inputNote").textContent=live?"所选论文将发送给已配置的视觉模型。":"本地抽取支持英文文本型论文及图注上方的图表。文件保存在本机。";
$("mode").textContent=live?"视觉模型在线模式":"本地运行";
form();
work("正在载入海报…",async()=>{
  const params=new URLSearchParams(location.search);
  if(params.get("example")==="forest" && $("sample").querySelector('option[value="forest"]')){
    $("sample").value="forest";
    $("sample").onchange();
    if(live) $("input").showModal(); else await generate();
    return;
  }
  const requested=params.get("session");
  const sid=requested || sessionStorage.getItem("studioSession");
  if(requested) window.history.replaceState(null,"",location.pathname);
  if(sid){try {display(await api("resume",{session:sid}));return;}catch{sessionStorage.removeItem("studioSession");}}
  if(live) $("input").showModal();
  else await generate();
});
