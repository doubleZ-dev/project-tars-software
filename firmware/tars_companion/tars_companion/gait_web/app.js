'use strict';
const authKey='tars-gait-auth-v1';
let token=location.hash.slice(1);
try{token=token||sessionStorage.getItem(authKey)||'';if(/^[a-f0-9]{64}$/.test(token))sessionStorage.setItem(authKey,token);}catch{}
if(!/^[a-f0-9]{64}$/.test(token))token='';
history.replaceState(null,'','/');
const $=id=>document.getElementById(id);
new MutationObserver(()=>{$('connection-error').textContent=$('error').textContent;}).observe($('error'),{childList:true,characterData:true,subtree:true});
const forwardNames=['起始','抬升中间','移动中间','落下中间','两侧回位','回起始'];
const turnNames=['起始','抬升两边','移动两边','落下两边','两边回位','回起始'];
const actionLabels={walk_forward:'前进',turn_left:'左转',turn_right:'右转'};
const drafts={};
let owner='',snapshot=null,metadata=null,frames=[],end=2,busy=false,pingBusy=false,devicesBusy=false;
const clone=v=>JSON.parse(JSON.stringify(v));
async function api(action,data={}){
 const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Tars-Token':token},body:JSON.stringify({owner,...data}),cache:'no-store'});
 const s=await r.json();if(!r.ok){if(s.snapshot)render(s.snapshot);if(r.status===403)throw Error('页面授权已失效：请关闭旧页面，从启动窗口的完整地址重新打开。不是USB故障。');throw Error(s.error||'请求失败');}return s;
}
function bounds(f,j){if(metadata.free_angles)return [-45,45];const base=f<6?metadata.baseline[f][j]:0;return f<6?[Math.min(0,base),Math.max(0,base)]:(metadata.extra_limits||[[0,10],[-45,45],[-45,45],[0,10]])[j];}
function valid(){return metadata&&Array.isArray(frames)&&frames.length>=6&&frames.length<=12&&end>=1&&end<=frames.length&&frames.every((row,f)=>Array.isArray(row)&&row.length===4&&row.every((v,j)=>{const [lo,hi]=bounds(f,j);return Number.isInteger(v)&&v>=lo&&v<=hi;}));}
function testOptions(){const turn=metadata?.action&&metadata.action!=='walk_forward',smooth=turn?Number($('turn-profile').value):0;return {drop_ramp:metadata?.action==='walk_forward'&&$('drop-ramp').checked,smooth,settle:turn&&smooth&&$('turn-settle').checked?60:0};}
function turnProblem(){const o=testOptions();if(!o.smooth)return '';if(!metadata?.turn_smooth)return '转向平滑需新版0.15.8-turnsmooth。';for(let f=2;f<Math.min(5,end);f++){const axes=f===3?[0,3]:[1,2];if(axes.some(j=>frames[f][j]!==frames[f-1][j]))return `第${f+1}阶段须保持${axes.map(j=>j+1).join('/')}号角度不变；独立调节可选直接轨迹。`;}return '';}
function controls(){const ready=!!owner&&snapshot?.connected&&!snapshot?.busy&&!busy;
 $('turn-profile').disabled=!ready||metadata?.action==='walk_forward';
 $('turn-settle').disabled=!ready||metadata?.action==='walk_forward'||!Number($('turn-profile').value);
 $('drop-ramp').disabled=!ready||metadata?.action!=='walk_forward';
 const rampSelected=metadata?.action==='walk_forward'&&$('drop-ramp').checked;
 const rampError=rampSelected&&(!metadata?.drop_ramp||end>=4&&[0,3].some(j=>frames[2][j]!==frames[3][j]));
 $('claim').disabled=!!owner;
 $('gait-action').disabled=!ready;
 for(const o of $('gait-action').options||[])o.disabled=o.value!=='walk_forward'&&!snapshot?.supports_turns;
 $('gait-action').value=snapshot?.action||metadata?.action||'walk_forward';
 $('connect').disabled=!owner||!!snapshot?.connected||busy||!$('devices').value;
 for(const id of ['run','zero'])$(id).disabled=!ready||!$('confirm').checked||(id==='run'&&!valid());
 if(rampError)$('run').disabled=true;
 const turnError=turnProblem();if(turnError)$('run').disabled=true;
 for(const id of ['append','duplicate'])$(id).disabled=!metadata||frames.length>=12||snapshot?.busy;
 $('remove').disabled=frames.length<=6||snapshot?.busy;
 $('baseline').disabled=!metadata||snapshot?.busy||busy;$('load').disabled=!metadata||snapshot?.busy||busy;
 // Confirmation may be checked without authority; it NEVER grants authority.
 $('confirm').disabled=!!snapshot?.busy||busy;
 const reason=busy||snapshot?.busy?'上一项操作处理中，请等待；关闭输出/急停仍可使用。':!token?'页面授权缺失，请从启动窗口完整地址重新打开。':!snapshot?.connected?'设备尚未连接：先在顶部选择USB并连接。':!owner?'网页控制权已释放：先在顶部点击“获取网页控制权”。':!valid()?'角度或阶段选择不合法，请先修正。':!$('confirm').checked?'请勾选上方安全确认框。':'已就绪：点击执行才会使能，并先回机械零位。';
 $('run-reason').textContent=reason;
 if(ready&&rampError)$('run-reason').textContent=!metadata?.drop_ramp?'平滑下降需烧录0.15.6-dropramp；可取消开关对照旧轨迹。':'请把第3、4阶段1号及4号角度分别设为一致，保证两侧不动。';
 if(ready&&turnError)$('run-reason').textContent=turnError;
 document.querySelectorAll('#rows input').forEach(el=>el.disabled=!snapshot?.connected||snapshot?.busy||busy||el.dataset.locked==='1');
 $('summary').textContent=frames.length?`${actionLabels[metadata?.action||'walk_forward']}：共${frames.length}阶段，选择1～${end}；指令用时约${(end*.3+(end>=4?testOptions().settle/1000:0)).toFixed(2)}秒后关闭输出。`:'请先连接并读取固件方案';
 $('turn-hint').textContent=snapshot?.connected&&!snapshot?.supports_turns?'当前固件只能调试前进；左右转阶段调试需烧录0.15.5-turnlab。':'切换步态只读取方案，不会连接、使能或执行动作。';
}
function updateAngles(){if(!metadata)return;
 document.querySelectorAll('[data-physical]').forEach(el=>{const [f,j]=el.dataset.physical.split(',').map(Number),v=frames[f][j];el.textContent=Number.isFinite(v)?`输出 ${metadata.zeros[j]+metadata.directions[j]*v}°`:'输出 —';});
 document.querySelectorAll('[data-delta]').forEach(el=>{const f=+el.dataset.delta,prev=f?frames[f-1]:[0,0,0,0];el.textContent=frames[f].map((v,j)=>v!==prev[j]?String(j+1):'').filter(Boolean).join('、')||'无';});
 controls();
}
function draw(){const body=$('rows');body.replaceChildren();if(!metadata)return;
 frames.forEach((row,f)=>{const tr=document.createElement('tr');tr.className=f<end?'selected':'';
 const choice=document.createElement('td'),check=document.createElement('input');check.type='checkbox';check.checked=f<end;check.setAttribute('aria-label',`执行阶段${f+1}`);check.onchange=()=>{end=check.checked?f+1:f;$('confirm').checked=false;draw();};choice.append(check);tr.append(choice);
 const names=metadata.action==='walk_forward'?forwardNames:turnNames;
 const label=document.createElement('td');label.textContent=`${f+1} ${names[f]||'自定义'}`;tr.append(label);
 row.forEach((v,j)=>{const td=document.createElement('td'),input=document.createElement('input'),out=document.createElement('span');const [lo,hi]=bounds(f,j);input.type='number';input.step='1';input.min=String(lo);input.max=String(hi);input.value=v;input.disabled=lo===hi;input.dataset.locked=lo===hi?'1':'0';input.setAttribute('aria-label',`阶段${f+1} 舵机${j+1} 逻辑角度`);input.oninput=()=>{frames[f][j]=input.value===''?NaN:Number(input.value);$('confirm').checked=false;updateAngles();};out.className='physical';out.dataset.physical=`${f},${j}`;td.append(input,out);tr.append(td);});
 const delta=document.createElement('td');delta.dataset.delta=f;tr.append(delta);body.append(tr);
 });updateAngles();
}
function render(s){snapshot=s;$('mode').textContent=s.mock?'MOCK · 虚拟设备，绝无硬件动作':'USB · 真实硬件需手动连接与确认';$('connection').textContent=s.connection;$('state').textContent=s.status+(!owner?'｜未持有网页控制权，请先点击获取':'');$('diagnostic').textContent=JSON.stringify(s.diagnostic||{},null,2);$('records').textContent=JSON.stringify((s.records||[]).slice(-4),null,2);
 if(s.error)$('error').textContent=s.error;
 if(s.metadata&&JSON.stringify(s.metadata)!==JSON.stringify(metadata)){if(metadata)drafts[metadata.action||'walk_forward']={metadata:clone(metadata),frames:clone(frames),end};metadata=clone(s.metadata);const saved=drafts[metadata.action||'walk_forward'];if(saved&&JSON.stringify(saved.metadata)===JSON.stringify(metadata)){frames=clone(saved.frames);end=saved.end;}else{frames=clone(metadata.baseline);end=2;}$('confirm').checked=false;draw();}
 $('geometry').textContent=s.connected&&metadata?`固件机械零点 [${metadata.zeros.join(', ')}]°；方向 [${metadata.directions.join(', ')}]；逻辑0°按此换算，实际位置未知。`:'未读取/连接失效：表中草稿不可发送，不代表当前实际位置';
 $('connect').disabled=!owner||s.connected||busy;$('disconnect').disabled=!owner||!s.connected;
 controls();
}
async function act(action,data={}){if(busy)return;busy=true;controls();$('error').textContent='';try{render(await api(action,data));}catch(e){$('error').textContent=e.message;}finally{busy=false;controls();}}
$('claim').onclick=async()=>{$('confirm').checked=false;try{const s=await api('claim');owner=s.owner;render(s);$('error').textContent='';}catch(e){$('error').textContent=e.message;}};
async function devices(){if(devicesBusy)return;devicesBusy=true;try{const chosen=$('devices').value,s=await api('devices');const options=s.devices.map(d=>{const o=document.createElement('option');o.value=d.serial;o.textContent=d.label;return o;});if(!options.length){const o=document.createElement('option');o.value='';o.textContent='未发现原生USB，等待设备重新出现';options.push(o);}$('devices').replaceChildren(...options);if(options.some(o=>o.value===chosen))$('devices').value=chosen;if(snapshot)render({...snapshot,mock:s.mock});if($('devices').value&&$('error').textContent==='请选择原生USB序列号')$('error').textContent='';controls();}catch(e){$('error').textContent=e.message;}finally{devicesBusy=false;}}
$('devices').onchange=controls;
$('gait-action').onchange=()=>{$('confirm').checked=false;act('select_action',{action:$('gait-action').value});};
$('refresh').onclick=devices;$('connect').onclick=()=>{$('confirm').checked=false;act('connect',{serial:$('devices').value});};$('disconnect').onclick=()=>act('disconnect');
$('confirm').onchange=controls;
$('drop-ramp').onchange=()=>{$('confirm').checked=false;controls();};
for(const id of ['turn-profile','turn-settle'])$(id).onchange=()=>{$('confirm').checked=false;controls();};
$('run').onclick=()=>{if(!valid()||!$('confirm').checked)return;const data={frames:clone(frames),selected:Array.from({length:end},(_,i)=>i+1),confirm:true,...testOptions()};$('confirm').checked=false;act('run',data);};
$('zero').onclick=()=>{if(!$('confirm').checked)return;$('confirm').checked=false;act('zero',{confirm:true});};
for(const id of ['off','estop'])$(id).onclick=()=>{ $('confirm').checked=false;api(id).then(render).catch(e=>{$('error').textContent=e.message;});};
$('clear').onclick=()=>act('clear');$('diag').onclick=()=>act('diag');
$('baseline').onclick=()=>{if(metadata){frames=clone(metadata.baseline);end=2;$('confirm').checked=false;draw();}};
$('append').onclick=()=>{if(metadata&&frames.length<12){frames.push([0,0,0,0]);$('confirm').checked=false;draw();}};
$('duplicate').onclick=()=>{if(metadata&&frames.length<12){frames.push(frames.at(-1).slice());$('confirm').checked=false;draw();}};
$('remove').onclick=()=>{if(frames.length>6){frames.pop();end=Math.min(end,frames.length);$('confirm').checked=false;draw();}};
function download(data,name){const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),500);}
$('save').onclick=()=>{if(valid())download({schema:'tars-gait-lab-1',metadata,frames,options:testOptions(),selected:Array.from({length:end},(_,i)=>i+1)},'tars-gait-plan.json');else $('error').textContent='请先连接并修正角度范围';};
$('export').onclick=async()=>{try{download(await api('export'),'tars-gait-evidence.json');}catch(e){$('error').textContent=e.message;}};
function restoreOptions(o){if(o===undefined)return;if(typeof o.drop_ramp!=='boolean'||![0,8,28,62].includes(o.smooth)||![0,60].includes(o.settle))throw Error('方案轨迹选项不合法');$('drop-ramp').checked=o.drop_ramp;$('turn-profile').value=String(o.smooth);$('turn-settle').checked=o.settle===60;}
$('load').onchange=async e=>{try{if(!metadata)throw Error('请先连接读取固件机械零点');const file=e.target.files[0];if(!file||file.size>65536)throw Error('方案文件超过64KB或未选择');const d=JSON.parse(await file.text());if(d.schema!=='tars-gait-lab-1'||JSON.stringify(d.metadata)!==JSON.stringify(metadata))throw Error('方案版本/机械零点/固件基线不匹配，拒绝导入');if(!Array.isArray(d.selected))throw Error('方案阶段不合法');const old=frames,oldEnd=end;frames=d.frames;end=d.selected.length;if(!d.selected.every((v,i)=>v===i+1)||!valid()){frames=old;end=oldEnd;throw Error('方案阶段或角度不合法');}restoreOptions(d.options);$('confirm').checked=false;draw();}catch(e){$('error').textContent=e.message;}e.target.value='';};
function release(){if(!owner)return;const old=owner;owner='';$('confirm').checked=false;controls();$('error').textContent='页面已失焦/隐藏，控制权已释放。回来后先点击“获取网页控制权”。';fetch('/api/release',{method:'POST',headers:{'Content-Type':'application/json','X-Tars-Token':token},body:JSON.stringify({owner:old}),keepalive:true}).then(async r=>{const s=await r.json();if(r.ok)render(s);}).catch(()=>{$('error').textContent='网页失活；请检查电源，固件租约将失能';});}
window.addEventListener('blur',release);window.addEventListener('pagehide',release);document.addEventListener('visibilitychange',()=>{if(document.hidden)release();});
setInterval(async()=>{if(!owner||document.hidden||!document.hasFocus()||pingBusy)return;pingBusy=true;try{render(await api('ping'));}catch(e){$('error').textContent=e.message;release();}finally{pingBusy=false;}},250);
// Enumeration only: never connect, Arm, resume or replay when a board reappears.
setInterval(()=>{if(token&&!document.hidden&&!busy&&!snapshot?.connected)devices();},2000);
render({mock:false,connected:false,status:'未连接，输出关闭',connection:'未连接',records:[]});
if(token)devices();else $('error').textContent='请用终端中带随机令牌的完整本机地址打开页面';
