"use strict";
const $ = id => document.getElementById(id);
const fa = new Intl.NumberFormat("fa-IR", {maximumFractionDigits: 1});
const n = value => value === null || value === undefined ? "—" : fa.format(value);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const phases = {clone:"دریافت مخزن",image:"ساخت محیط Docker",prepare:"آماده‌سازی محیط",install:"نصب وابستگی‌ها",tests:"تست‌های مخزن",server:"شروع سرور آزمایشی",browser:"بررسی صفحات و سناریوها",load:"آزمون فشار HTTP",report:"آماده‌سازی گزارش",completed:"آزمون تکمیل شد",failed:"اجرای ناقص",cancelled:"متوقف شد",interrupted:"اجرای قطع‌شده",stopping:"در حال توقف…"};
const states = {completed:"تکمیل‌شده",failed:"اجرای ناقص",cancelled:"متوقف‌شده",interrupted:"قطع‌شده",running:"در حال اجرا"};
const labels = {pass:"موفق",fail:"ناموفق",warn:"هشدار",skip:"اجرا نشد"};
const categories = {setup:"آماده‌سازی",infrastructure:"محیط اجرا",configuration:"پیکربندی",application:"برنامه",runtime:"اجرای برنامه",unit:"تست مخزن",static:"تحلیل کد",http:"صفحات",javascript:"JavaScript",responsive:"نمای موبایل",authentication:"ورود",scenario:"سناریو",performance:"کارایی",load:"فشار HTTP",browser:"مرورگر"};
const navTitles = {overview:"نمای کلی",checks:"نتایج و خطاها",resources:"مصرف منابع",logs:"خروجی اجرا",history:"تاریخچه آزمون‌ها"};
let health = null, currentJob = null, selectedId = null, currentView = "overview", busy = false, toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options,
    headers:{"Content-Type":"application/json", "X-QA-Token":health?.session_token || "", ...options.headers}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function toast(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 4500);
}
function view(name) {
  currentView = name;
  document.querySelectorAll(".view").forEach(el => el.hidden = el.id !== `view-${name}`);
  document.querySelectorAll("[data-nav]").forEach(el => el.classList.toggle("active", el.dataset.nav === name));
  $("pageTitle").textContent = navTitles[name];
  $("heading").textContent = name === "overview" ? "از مخزن تا گزارش خطا." : navTitles[name];
  if (name === "history") refreshHistory().catch(e => toast(e.message));
  requestAnimationFrame(drawCharts);
}
document.querySelectorAll("[data-nav]").forEach(button => button.addEventListener("click", () => view(button.dataset.nav)));
$("showLogsButton").addEventListener("click", () => view("logs"));
$("resourceDetails").addEventListener("click", () => view("resources"));
$("helpButton").addEventListener("click", () => $("helpDialog").showModal());
$("healthHelp").addEventListener("click", () => $("helpDialog").showModal());
$("closeHelp").addEventListener("click", () => $("helpDialog").close());
$("helpDialog").addEventListener("click", e => { if (e.target === $("helpDialog")) { const r=e.target.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close(); }});
$("checkFilter").addEventListener("change", renderChecks);
$("resourceScope").addEventListener("change", () => { renderResources(); drawCharts(); });
$("refreshHistory").addEventListener("click", () => refreshHistory().catch(e => toast(e.message)));
$("copyLogs").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("logs").textContent); toast("خروجی کپی شد."); } catch { toast("امکان کپی خودکار وجود ندارد؛ متن خروجی را انتخاب و کپی کنید."); } });
function startState() {
  $("startButton").disabled = busy || !health?.ready || !!health?.preview || !!health?.active_job || (currentJob && !terminal.has(currentJob.status));
}
async function refreshHealth() {
  try {
    health = await api("/api/health");
    const badge = $("healthBadge");
    badge.className = `heading-status ${health.ready ? "ready" : "warning"}`;
    badge.lastElementChild.textContent = health.ready ? "محیط اجرا آماده است" : "محیط اجرا نیاز به آماده‌سازی دارد";
    badge.title = health.detail;
    $("healthNotice").hidden = health.ready;
    $("healthText").textContent = health.detail;
    $("previewNotice").hidden = !health.preview;
    startState();
  } catch (e) {
    $("healthBadge").className = "heading-status warning";
    $("healthBadge").lastElementChild.textContent = "اتصال به سرویس برقرار نیست";
    $("healthNotice").hidden = false;
    $("healthText").textContent = "سرویس محلی در دسترس نیست. فایل راه‌انداز را دوباره اجرا کنید.";
    health = null; startState();
  }
}
function config() {
  let scenarios;
  try { scenarios = JSON.parse($("scenarios").value || "[]"); } catch { throw new Error("JSON سناریوها معتبر نیست؛ براکت‌ها و ویرگول‌ها را بررسی کنید."); }
  return {
    repo_url:$("repoUrl").value.trim(), branch:$("branch").value.trim(), profile:$("profile").value,
    users:Number($("users").value), duration:Number($("duration").value), max_pages:Number($("maxPages").value),
    run_tests:$("runTests").checked, run_browser:$("runBrowser").checked, run_load:$("runLoad").checked,
    run_deploy_check:$("runDeployCheck").checked, subdir:$("subdir").value.trim(), django_settings:$("djangoSettings").value.trim(),
    paths:$("paths").value.split("\n").map(x=>x.trim()).filter(Boolean), memory_mb:Number($("memory").value),
    cpus:Number($("cpus").value), timeout_minutes:Number($("timeout").value), slow_ms:Number($("slowMs").value),
    setup_command:$("setupCommand").value, test_command:$("testCommand").value, start_command:$("startCommand").value, scenarios
  };
}
function applyConfig(data) {
  const ids = {repo_url:"repoUrl",branch:"branch",profile:"profile",users:"users",duration:"duration",max_pages:"maxPages",subdir:"subdir",django_settings:"djangoSettings",memory_mb:"memory",cpus:"cpus",timeout_minutes:"timeout",slow_ms:"slowMs",setup_command:"setupCommand",test_command:"testCommand",start_command:"startCommand"};
  for (const [key,id] of Object.entries(ids)) if(data[key]!==undefined) $(id).value = data[key];
  const checks = {run_tests:"runTests",run_browser:"runBrowser",run_load:"runLoad",run_deploy_check:"runDeployCheck"};
  for (const [key,id] of Object.entries(checks)) if(typeof data[key]==="boolean") $(id).checked=data[key];
  if(Array.isArray(data.paths)) $("paths").value=data.paths.join("\n");
  if(Array.isArray(data.scenarios)) $("scenarios").value=JSON.stringify(data.scenarios,null,2);
}
$("runForm").addEventListener("submit", async e => {
  e.preventDefault(); $("formError").hidden=true;
  try {
    const payload = config(); busy=true; startState();
    currentJob = await api("/api/jobs", {method:"POST", body:JSON.stringify(payload)});
    selectedId=currentJob.id; renderJob(); toast("آزمون شروع شد؛ دریافت مخزن در حال انجام است.");
  } catch(e) { $("formError").textContent=e.message; $("formError").hidden=false; }
  finally { busy=false; startState(); }
});
$("cancelButton").addEventListener("click", async () => {
  if(!currentJob) return;
  try { currentJob=await api(`/api/jobs/${currentJob.id}/cancel`,{method:"POST",body:"{}"}); renderJob(); toast("درخواست توقف ثبت شد؛ گزارش مراحل انجام‌شده حفظ می‌شود."); } catch(e) { toast(e.message); }
});
$("exportConfig").addEventListener("click", () => {
  try { const url=URL.createObjectURL(new Blob([JSON.stringify(config(),null,2)],{type:"application/json"})); const a=document.createElement("a"); a.href=url;a.download="azmayar-config.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),2000); } catch(e) {toast(e.message);}
});
$("importConfig").addEventListener("change", async e => {
  try { const file=e.target.files[0];if(!file)return;if(file.size>256000)throw new Error("حجم تنظیمات بیش از حد مجاز است."); const data=JSON.parse(await file.text()); if(!data||typeof data!=="object"||Array.isArray(data))throw new Error("فایل تنظیمات معتبر نیست.");applyConfig(data);toast("تنظیمات وارد شد. پیش از شروع، آن را بررسی کنید."); } catch(e) {toast(e.message);} finally {e.target.value="";}
});
$("exampleScenario").addEventListener("click", () => {
  $("scenarios").value=JSON.stringify([{name:"ثبت و جست‌وجوی مشتری در دیدبان",steps:[
    {action:"goto",value:"/customers/new/"},
    {action:"fill",selector:'input[name="name"]',value:"مشتری آزمون آزمایار"},
    {action:"fill",selector:'input[name="phone"]',value:"09120009999"},
    {action:"click",selector:'form.form-card button[type="submit"]'},
    {action:"goto",value:"/customers/?q=09120009999"},
    {action:"expect_text",selector:"body",value:"مشتری آزمون آزمایار"}
  ]}],null,2);
  toast("نمونه اضافه شد. این سناریو فقط دادهٔ آزمایشی ایجاد می‌کند.");
});
function renderJob() {
  const job=currentJob; if(!job)return;
  const done=terminal.has(job.status), summary=job.summary||{};
  $("executionEmpty").hidden=true; $("executionActive").hidden=false;
  $("jobRepo").textContent=job.config.repo_url.replace("https://","").replace(/\.git$/,"");
  $("jobState").textContent=states[job.status]||job.status;
  $("jobState").className=`pill ${job.status==="completed" ? ((summary.fail||0)>0?"warn":"pass") : (job.status==="running"?"running":"fail")}`;
  $("phaseLabel").textContent=phases[job.phase]||job.phase;
  $("progressText").textContent=n(job.progress)+"٪"; $("progressFill").style.width=`${Math.min(100,Math.max(0,Number(job.progress)||0))}%`;
  $("commitInfo").textContent=job.commit ? `COMMIT ${job.commit.slice(0,12)} · ${job.profile||"detecting…"}` : "COMMIT —";
  $("cancelButton").hidden=done;$("cancelButton").disabled=job.phase==="stopping";
  const unit=job.unit_tests;
  $("unitSummary").textContent=unit ? `${n(unit.total)} تست مخزن: ${n(unit.passed)} موفق، ${n(unit.failures+unit.errors)} ناموفق، ${n(unit.skipped)} اجرا نشده` : "تعداد تست‌های داخلی، در صورت گزارش توسط Django، اینجا نمایش داده می‌شود.";
  const steps=["clone","install","tests","browser","load","report"];
  const phaseMap={image:"install",prepare:"install",server:"browser",completed:"report"};
  const index=steps.indexOf(phaseMap[job.phase]||job.phase);
  document.querySelectorAll(".pipeline [data-phase]").forEach((el,i)=>{el.classList.toggle("current",!done&&i===index);el.classList.toggle("done",job.status==="completed"||i<index);});
  $("statPassed").textContent=n(summary.pass||0);$("statFailed").textContent=n(summary.fail||0);
  $("statTotal").textContent=`از ${n((job.checks||[]).length)} بررسی ثبت‌شده`;
  $("statWarnings").textContent=`${n(summary.warn||0)} هشدار · ${n(summary.skip||0)} اجرا نشده`;
  $("navIssueCount").textContent=n(summary.fail||0);
  const r=job.resource_summary?.application||{};
  $("statCpu").textContent=n(r.cpu_peak);$("statMemory").textContent=n(r.memory_peak_mb);
  const log=$("logs"), atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-80;
  log.textContent=job.logs?.join("\n")||"Waiting for worker output…";
  if(atBottom) log.scrollTop=log.scrollHeight;
  renderChecks();renderResources();renderDownloads();drawCharts();renderTime();startState();
}
function renderTime() {
  if(!currentJob)return;
  const seconds=terminal.has(currentJob.status)?currentJob.elapsed_s:Math.max(0,(Date.now()-Date.parse(currentJob.created_at))/1000);
  if(seconds!==undefined)$("elapsed").textContent=`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(Math.floor(seconds%60)).padStart(2,"0")}`;
}
function renderChecks() {
  const filter=$("checkFilter").value;
  const opened=new Set([...$("checksList").querySelectorAll("details[open]")].map(el=>el.dataset.checkId));
  const checks=(currentJob?.checks||[]).filter(c=>filter==="all"||c.status===filter);
  if(!checks.length){$("checksList").innerHTML='<div class="empty-state">برای این انتخاب، نتیجه‌ای ثبت نشده است.</div>';return;}
  $("checksList").innerHTML=checks.map(c=>`<details class="check-item" data-check-id="${esc(c.id||c.name)}" ${c.status==="fail"||opened.has(c.id||c.name)?"open":""}><summary class="check-title"><span class="pill ${esc(c.status)}">${esc(labels[c.status])}</span><h3>${esc(c.name)}</h3><span class="category">${esc(categories[c.category]||c.category)}</span></summary><pre>${esc(c.details)}</pre></details>`).join("");
}
function fileUrl(name){return `/api/jobs/${selectedId}/files/${encodeURIComponent(name)}`;}
function renderDownloads() {
  const done=currentJob&&terminal.has(currentJob.status);
  $("downloads").innerHTML=done ? [["report.html","گزارش HTML"],["report.json","JSON"],["resources.csv","منابع CSV"]].map(([file,label])=>`<a class="small-button" href="${fileUrl(file)}" download><svg><use href="#i-download"/></svg>${label}</a>`).join("") : '<span class="muted">پس از پایان اجرا فعال می‌شود.</span>';
  const shots=currentJob?.screenshots||[];
  const signature=`${selectedId}/${shots.join(",")}`;
  if($("screenshots").dataset.signature!==signature){$("screenshots").dataset.signature=signature;$("screenshots").innerHTML=shots.map(s=>`<a href="${fileUrl(s)}" target="_blank" rel="noopener"><img loading="lazy" src="${fileUrl(s)}" alt="${esc(s)}"><span>${esc(s)}</span></a>`).join("")||'<span class="muted">پس از پایان بررسی مرورگر، تصاویر اینجا نمایش داده می‌شوند.</span>';}
}
function renderResources() {
  const scope=$("resourceScope").value;
  const hints={application:"فقط فرایندهای سرور؛ مرورگر و نصب در این آمار نیستند. حافظه مجموع RSS است.",container:"کل کانتینر، شامل برنامه، مرورگر، نصب و تست‌ها. CPU: هر ۱۰۰٪ یک هسته.",host:"کل مصرف سیستم میزبان، شامل سایر برنامه‌ها. CPU: درصد کل ظرفیت سیستم."};
  $("scopeHint").textContent=hints[scope];
  const r=currentJob?.resource_summary?.[scope]||{};
  $("resourceNumbers").innerHTML=`<span>میانگین CPU <b>${n(r.cpu_avg)}٪</b></span><span>بیشینه CPU <b>${n(r.cpu_peak)}٪</b></span><span>بیشینه RAM <b>${n(r.memory_peak_mb)} MiB</b></span><span>نمونه <b>${n(r.samples||0)}</b></span>`;
  const load=currentJob?.load;
  if(!load){$("loadMetrics").innerHTML='<div class="empty-state">هنوز آزمون فشار اجرا نشده است.</div>';return;}
  const fields=[["درخواست",load.requests],["خطا",load.errors],["نرخ خطا ٪",load.error_percent],["درخواست بر ثانیه",load.rps],["زمان پاسخ P50 · ms",load.p50_ms],["زمان پاسخ P95 · ms",load.p95_ms],["زمان پاسخ P99 · ms",load.p99_ms],["کاربر HTTP هم‌زمان",load.users]];
  $("loadMetrics").innerHTML=fields.map(([k,v])=>`<div>${esc(k)}<strong>${n(v)}</strong></div>`).join("");
}
function chart(canvas, samples, emptyId) {
  if(!canvas.clientWidth)return;
  const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
  canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;
  const c=canvas.getContext("2d");c.scale(dpr,dpr);
  const w=rect.width,h=rect.height,L=43,R=60,T=21,B=28;
  const points=samples.filter(s=>Number.isFinite(s.cpu_percent)&&Number.isFinite(s.memory_mb));
  $(emptyId).hidden=points.length>0;
  const cpuMax=Math.max(100,...points.map(p=>p.cpu_percent))*1.1;
  const memMax=Math.max(100,...points.map(p=>p.memory_mb))*1.1;
  const maxTime=Math.max(10,...points.map(p=>p.elapsed_s||0));
  c.font="9px Arial";c.lineWidth=1;
  for(let i=0;i<5;i++){
    const y=T+(h-T-B)*i/4;c.beginPath();c.strokeStyle="#eef2f8";c.setLineDash([3,4]);c.moveTo(L,y);c.lineTo(w-R,y);c.stroke();
    c.fillStyle="#b0bad0";c.textAlign="right";c.fillText(Math.round(cpuMax*(1-i/4))+"%",L-9,y+3);
    c.textAlign="left";c.fillText(Math.round(memMax*(1-i/4))+" MiB",w-R+8,y+3);
  }
  c.setLineDash([]);c.fillStyle="#adb9ce";
  for(let i=0;i<5;i++){c.textAlign="center";c.fillText(`${Math.round(maxTime*i/4)}s`,L+(w-L-R)*i/4,h-6);}
  if(!points.length)return;
  function line(key,max,color,fill){
    const coords=points.map(p=>[L+(p.elapsed_s||0)/maxTime*(w-L-R),T+(1-p[key]/max)*(h-T-B)]);
    if(fill){const gradient=c.createLinearGradient(0,T,0,h-B);gradient.addColorStop(0,"#5b7df024");gradient.addColorStop(1,"#5b7df001");c.beginPath();c.moveTo(coords[0][0],h-B);coords.forEach(([x,y])=>c.lineTo(x,y));c.lineTo(coords.at(-1)[0],h-B);c.closePath();c.fillStyle=gradient;c.fill();}
    c.beginPath();c.strokeStyle=color;c.lineWidth=1.8;coords.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));c.stroke();
    if(coords.length===1){c.fillStyle=color;c.beginPath();c.arc(coords[0][0],coords[0][1],2.5,0,Math.PI*2);c.fill();}
  }
  line("cpu_percent",cpuMax,"#6d88ed",true);line("memory_mb",memMax,"#54bca8",false);
}
function drawCharts(){const rows=currentJob?.resources||[];chart($("overviewChart"),rows.filter(s=>s.scope==="application"),"overviewChartEmpty");chart($("resourcesChart"),rows.filter(s=>s.scope===$("resourceScope").value),"resourcesChartEmpty");}
new ResizeObserver(drawCharts).observe(document.querySelector(".content"));
async function refreshHistory() {
  const rows=await api("/api/jobs");
  $("historyRows").innerHTML=rows.length?rows.map(job=>`<tr><td dir="ltr">${esc(job.config.repo_url.replace(/^https:\/\//,"").replace(/\.git$/,""))}</td><td>${esc(new Date(job.created_at).toLocaleString("fa-IR",{dateStyle:"short",timeStyle:"short"}))}</td><td><span class="pill ${job.status==="running"?"running":job.status==="completed"?"pass":"fail"}">${esc(states[job.status]||job.status)}</span></td><td>${n(job.summary?.fail||0)}</td><td class="code">${esc(job.commit?.slice(0,8)||"—")}</td><td><button class="small-button" data-job="${esc(job.id)}">مشاهده</button></td></tr>`).join(""):'<tr><td colspan="6" class="empty-state">هنوز آزمونی ثبت نشده است.</td></tr>';
  document.querySelectorAll("[data-job]").forEach(button=>button.addEventListener("click",async()=>{try{selectedId=button.dataset.job;currentJob=await api(`/api/jobs/${selectedId}`);renderJob();view("overview");}catch(e){toast(e.message);}}));
  return rows;
}
async function poll() {
  if(!selectedId)return;
  try {const job=await api(`/api/jobs/${selectedId}`);const wasRunning=currentJob&&!terminal.has(currentJob.status);currentJob=job;renderJob();if(wasRunning&&terminal.has(job.status)){toast(job.status==="completed"?"آزمون پایان یافت؛ گزارش آماده است.":"اجرا پایان یافت؛ جزئیات را در گزارش بررسی کنید.");refreshHealth();}} catch(e){toast(e.message);}
}
async function boot(){await refreshHealth();try{const rows=await refreshHistory();const active=rows.find(x=>x.status==="running")||rows[0];if(active){selectedId=active.id;currentJob=await api(`/api/jobs/${selectedId}`);renderJob();}}catch(e){toast(e.message);}drawCharts();}
boot();
setInterval(()=>{if(currentJob&&!terminal.has(currentJob.status))poll();},2000);
setInterval(refreshHealth,15000);setInterval(renderTime,1000);
