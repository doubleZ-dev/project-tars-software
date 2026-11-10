// Real local Chromium DOM flow against the offline Mock bridge. No serial access.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const base=path.resolve(__dirname,'..');
const evidence=path.join(base,'evidence');fs.mkdirSync(evidence,{recursive:true});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let pc,chrome,ws;
const pending=new Map();let seq=0;
function send(method,params={}){console.log('CDP',method);const id=++seq;return new Promise((resolve,reject)=>{setTimeout(()=>reject(Error('CDP timeout '+method)),12000).unref();pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
(async()=>{
 try{
  pc=spawn('python',['-u','-m','tars_companion.servo_calibration','--mock','--port','8767'],{cwd:base,windowsHide:true});
  let out='';const url=await new Promise((resolve,reject)=>{pc.stdout.on('data',b=>{out+=b.toString();const m=out.match(/http:\/\/127\.0\.0\.1:8767\/#[a-f0-9]+/);if(m)resolve(m[0]);});pc.on('exit',c=>reject(Error('bridge exited '+c)));setTimeout(()=>reject(Error('bridge startup timeout')),8000).unref();});
  console.log('Bridge ready',url);
  chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-sandbox','--disable-gpu-sandbox','--disable-software-rasterizer','--disable-features=RendererCodeIntegrity','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=9337',`--user-data-dir=${path.join(evidence,'chrome-servo-profile-'+Date.now())}`,'about:blank'],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  chrome.stderr.on('data',()=>{});
  chrome.on('exit',c=>console.log('Chromium exit',c));
  let targets;
  for(let i=0;i<50;i++){try{targets=await(await fetch('http://127.0.0.1:9337/json/list',{signal:AbortSignal.timeout(500)})).json();break;}catch{await sleep(100);}}
  console.log('Targets',targets?.length);
  assert.ok(targets,'Chromium CDP startup');
  const target=targets.find(t=>t.type==='page');ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;setTimeout(()=>reject(Error('WebSocket startup timeout')),3000).unref();});
  ws.onmessage=e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}};
  await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url});await sleep(350);await send('Page.bringToFront');
  const result=await evaluate(`(async()=>{
    const wait=ms=>new Promise(r=>setTimeout(r,ms));
    const check=(v,label)=>{if(!v)throw Error(label);return label;};const passed=[];
    passed.push(check(document.querySelectorAll('.card').length===4,'four GPIO-labelled servo cards'));
    passed.push(check(document.hasFocus(),'page focused'));
    document.getElementById('claim').click();await wait(100);
    document.getElementById('connect').click();await wait(150);
    passed.push(check(document.getElementById('connection').textContent.includes('Mock'),'real Mock handshake surfaced'));
    const card=document.querySelector('.card');
    card.querySelector('.safe').click();card.querySelector('.enable').click();await wait(150);
    passed.push(check(document.getElementById('state').textContent.includes('Servo1'),'explicit single-axis enable'));
    const before=card.querySelector('.target').textContent;
    const range=card.querySelector('[type=range]');range.value='7';range.dispatchEvent(new Event('input',{bubbles:true}));await wait(350);
    passed.push(check(card.querySelector('.target').textContent===before,'slider edits draft without dispatch'));
    card.querySelector('.send').click();await wait(130);
    passed.push(check(card.querySelector('.target').textContent.includes('7° / 1577μs'),'send uses firmware mapping and completed response'));
    card.querySelector('.zero-confirm').click();card.querySelector('.mark').click();await wait(120);
    passed.push(check(JSON.parse(document.getElementById('records').textContent)[0].user_confirmed_mechanical_zero_deg===7,'manual mechanical-zero record'));
    const exported=await api('export');passed.push(check(exported.mock && exported.records[0].actual_angle_deg===null,'JSON export marked mock and actual angle unknown'));
    card.querySelector('.center').click();await wait(120);
    passed.push(check(card.querySelector('.target').textContent.includes('0° / 1500μs'),'reference center'));
    document.getElementById('off').click();await wait(100);
    passed.push(check(document.getElementById('state').textContent==='未使能','global output off'));
    card.querySelector('.enable').click();await wait(120);
    window.dispatchEvent(new Event('blur'));await wait(120);
    passed.push(check(!owner,'blur revokes local page ownership'));
    document.getElementById('claim').click();await wait(100);
    const status=await api('ping');passed.push(check(!status.status.active,'blur closes Mock output'));
    document.getElementById('disconnect').click();await wait(100);
    document.getElementById('connect').click();await wait(130);
    passed.push(check(!snapshot.status.active && !snapshot.last_target,'reconnect does not enable or replay'));
    return passed;
  })()`);
  const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(path.join(evidence,'servo-page-mock.png'),Buffer.from(shot.data,'base64'));
  fs.writeFileSync(path.join(evidence,'servo-browser.json'),JSON.stringify({mock:true,passed:result},null,2));console.log(JSON.stringify({passed:result.length,checks:result},null,2));
 }finally{
  if(ws)ws.close();if(chrome)chrome.kill();if(pc)pc.kill();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
