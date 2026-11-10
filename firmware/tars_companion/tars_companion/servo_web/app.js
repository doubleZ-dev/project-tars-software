'use strict';
const token = location.hash.slice(1);
history.replaceState(null, '', '/');
let owner = '', snapshot = null, busy = false, pingBusy = false;
const $ = id => document.getElementById(id);
const cards = [];
async function api(action, data = {}) {
  const response = await fetch('/api/' + action, {method:'POST', headers:{'Content-Type':'application/json','X-Tars-Token':token}, body:JSON.stringify({owner,...data}), cache:'no-store'});
  const result = await response.json();
  if (!response.ok) { if(result.snapshot) render(result.snapshot); throw new Error(result.error || '请求失败'); }
  return result;
}
function render(s) {
  snapshot = s;
  $('connection').textContent = s.connection;
  $('mode').textContent = s.mock ? 'MOCK · 虚拟设备，仅验证软件；没有硬件执行' : 'USB · 仅根据固件响应显示已下发目标，实际角度未知';
  const state = s.status || {};
  $('state').textContent = state.closing ? '关闭请求已发送 · 等待设备确认；输出状态未知' : state.connection_error ? '连接异常 · 输出状态未知' : state.estop ? '急停 · 未使能' : state.timed_out ? '超时 · 未使能' : state.active ? `校准使能 · Servo${state.servo}` : '未使能';
  cards.forEach((card, i) => {
    const position = s.servo_positions?.[String(i+1)];
    card.node.querySelector('h2').textContent = `Servo${i+1} · GPIO${i+38}${position ? ' · '+position : ''}`;
    const active = state.active && state.servo === i+1;
    card.node.classList.toggle('selected',active);
    card.enable.disabled = !owner || !s.connected || state.estop || busy;
    [card.send,card.center,card.mark].forEach(b => b.disabled = !owner || !active || busy);
    const t = s.last_target;
    card.target.textContent = t && t.servo === i+1 ? `已下发目标：${t.calibration_angle_deg}° / ${t.pulse_us}μs（${s.mock?'Mock 响应':'固件完成响应'}）；实际角度未知` : '尚无当前有效目标；实际角度未知';
  });
  $('records').textContent = s.records.length ? JSON.stringify(s.records,null,2) : '暂无记录';
}
async function act(action, data={}) {
  if (busy) return;
  if (['begin','set'].includes(action)) document.querySelectorAll('.zero-confirm').forEach(c=>c.checked=false);
  busy=true; if(snapshot) render(snapshot); $('error').textContent='';
  try { render(await api(action,data)); }
  catch(e) { $('error').textContent=e.message; }
  finally { busy=false; if(snapshot) render(snapshot); }
}
for(let i=1;i<=4;i++){
  const node = document.createElement('article'); node.className='card';
  node.innerHTML=`<h2>Servo${i} <small>· GPIO${i+37}</small></h2><p>相对参考中位的校准角（不是步态逻辑角）</p><p><label><input type="checkbox" class="safe"> 我已拆开舵臂和连杆，确认此舵机可安全居中</label></p><button class="enable">选择该舵机并确认校准使能</button><p><label>草稿角度 <input type="number" min="-20" max="20" step="1" value="0" aria-label="Servo${i} 草稿角度"> °</label></p><input type="range" min="-20" max="20" step="1" value="0" aria-label="Servo${i} 角度滑块"><div class="toolbar"><button class="minus" aria-label="Servo${i} 减少1度">−1°</button><button class="plus" aria-label="Servo${i} 增加1度">+1°</button><button class="send">发送目标角度</button><button class="center">回参考中位</button><button class="mark">标记为机械零点</button></div><p class="target"></p><label><input type="checkbox" class="zero-confirm"> 我已人工确认当前目标对应机构机械零点</label>`;
  $('cards').appendChild(node);
  const find=c=>node.querySelector('.'+c), number=node.querySelector('[type=number]'), range=node.querySelector('[type=range]');
  const draft=value=>{number.value=String(value);range.value=String(value);};
  number.addEventListener('input',()=>{if(number.validity.valid)range.value=number.value;});
  range.addEventListener('input',()=>{number.value=range.value;});
  find('minus').onclick=()=>draft(Math.max(-20,Number(number.value)-1));
  find('plus').onclick=()=>draft(Math.min(20,Number(number.value)+1));
  find('enable').onclick=()=>{if(find('safe').checked)act('begin',{servo:i,confirm:true});};
  find('send').onclick=()=>{if(number.reportValidity() && number.value!=='')act('set',{servo:i,angle_deg:Number(number.value)});};
  find('center').onclick=()=>{draft(0);act('set',{servo:i,angle_deg:0});};
  find('mark').onclick=()=>{if(find('zero-confirm').checked){act('mark',{servo:i,confirm:true});find('zero-confirm').checked=false;}};
  cards.push({node,enable:find('enable'),send:find('send'),center:find('center'),mark:find('mark'),target:find('target')});
}
$('claim').onclick=async()=>{try{const s=await api('claim'); owner=s.owner; render(s); $('error').textContent='';}catch(e){$('error').textContent=e.message;}};
async function devices(){try{const s=await api('devices');if(snapshot)render({...snapshot,servo_positions:s.servo_positions});$('devices').replaceChildren(...s.devices.map(d=>{const o=document.createElement('option');o.value=d.serial;o.textContent=d.label;return o;}));}catch(e){$('error').textContent=e.message;}}
$('refresh').onclick=devices;
$('connect').onclick=()=>act('connect',{serial:$('devices').value});
$('disconnect').onclick=()=>act('disconnect');
$('off').onclick=()=>api('off').then(render).catch(e=>{$('error').textContent=e.message;}); $('estop').onclick=()=>api('estop').then(render).catch(e=>{$('error').textContent=e.message;}); $('clear').onclick=()=>act('clear');
$('export').onclick=async()=>{try{const data=await api('export');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='tars-mechanical-zero-'+new Date().toISOString().replaceAll(':','-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){$('error').textContent=e.message;}};
function release(){
  if(!owner)return;
  const old=owner;owner='';
  if(snapshot){snapshot.status={...snapshot.status,closing:true};snapshot.last_target=null;render(snapshot);}
  fetch('/api/release',{method:'POST',headers:{'Content-Type':'application/json','X-Tars-Token':token},body:JSON.stringify({owner:old}),keepalive:true})
    .then(async r=>{const data=await r.json();if(r.ok)render(data);else if(data.snapshot)render(data.snapshot);})
    .catch(()=>{if(snapshot){snapshot.connected=false;snapshot.connection='本地服务连接异常';snapshot.status={connection_error:true};render(snapshot);}});
  $('error').textContent='页面失活，已请求关闭输出；请重新获取网页控制权。固件独立租约约1.2秒，另加看门狗调度时间。';
}
window.addEventListener('blur',release);
window.addEventListener('pagehide',release);
document.addEventListener('visibilitychange',()=>{if(document.hidden)release();});
setInterval(async()=>{if(!owner||document.hidden||!document.hasFocus()||pingBusy)return;pingBusy=true;try{render(await api('ping'));}catch(e){$('error').textContent=e.message;release();}finally{pingBusy=false;}},250);
render({mock:false,connection:'未连接',connected:false,status:{active:false},records:[],last_target:null});
if(!token)$('error').textContent='请从启动终端打开带随机令牌的完整地址；刷新页面后也需重新打开该地址。';
else devices();
