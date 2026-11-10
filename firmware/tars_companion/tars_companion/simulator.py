from __future__ import annotations

import asyncio
import json
import secrets
from collections import OrderedDict
from typing import Any

from .transport import LineTransport


class SimulatedTarsDevice:
    def __init__(self, transport: LineTransport, motion_delay: float = 0.02,
                 lease_timeout: float = 2.0, native_session_required: bool = False, gait_testing: bool = False):
        self.transport = transport
        self.motion_delay = motion_delay
        self.cache: OrderedDict[str, tuple[bytes, bytes]] = OrderedDict()
        self.execution_count = 0
        self.dialogue: list[tuple[str, str]] = []
        self.estop_latched = False
        self.armed = False
        self.active_id: str | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self.lease_timeout = lease_timeout
        self._lease_deadline = 0.0
        self.boot_id = secrets.token_hex(8)
        self._rebooting = False
        self.native_session_required = native_session_required
        self.gait_testing=gait_testing
        self.gait_trace={'valid':True,'stage':'boot','action':-1,'cycle':0,'phase':0,'mark_ms':0,'command_id':''}
        self.gait_test_history=[]
        self.session_id = ""
        self.calibration = {"active": False, "timed_out": False, "estop": False,
                            "servo": 0, "angle_deg": 0, "pulse_us": 1500}
        self.calibration_id = ""
        self.calibration_deadline = 0.0
        self.calibration_writes = 0
        self.outputs = [False] * 4
        self.interaction_state = "IDLE"
        self.interaction_history: list[str] = []

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="simulated-tars")
        self._lease_task = asyncio.create_task(self._lease_watchdog(), name="simulated-tars-lease")
        await self._hello()

    async def _hello(self) -> None:
        await self._send({"v": 2, "type": "hello", "boot_id": self.boot_id,
                          "payload": {"device_id": "sim-tars", "protocols": [2],
                "firmware_version": "0.15.9-voicegait" if self.gait_testing else "0.14.0-stage4",
                "release_id": "tars-wireless-voicegait-v1" if self.gait_testing else "tars-pcb-v1-stage4",
                                      "boot_disarmed": True, "session_id": self.session_id}})

    async def reboot(self) -> None:
        self._rebooting = True
        if self._motion_task:
            self._motion_task.cancel()
            await asyncio.gather(self._motion_task, return_exceptions=True)
        self._calibration_off()
        self.cache.clear()
        self.active_id = None
        self.estop_latched = False
        self.armed = False
        self._lease_deadline = 0.0
        self.boot_id = secrets.token_hex(8)
        self.session_id = ""
        self._rebooting = False
        await self._hello()

    async def close(self) -> None:
        if self._lease_task:
            self._lease_task.cancel()
            await asyncio.gather(self._lease_task, return_exceptions=True)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _send(self, message: dict[str, Any]) -> bytes:
        wire = json.dumps(message, separators=(",", ":")).encode()
        await self.transport.write_line(wire)
        return wire

    async def _reply(self, command_id: str, msg_type: str, state: str, **extra: Any) -> bytes:
        return await self._send({"v": 2, "type": msg_type, "boot_id": self.boot_id,
                                 "session_id": self.session_id,
                                 "command_id": command_id, "state": state, **extra})

    async def _run(self) -> None:
        while True:
            wire = await self.transport.read_line()
            try:
                message = json.loads(wire)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            command_id = message.get("command_id")
            if message.get("type") == "session.open":
                session_id = message.get("payload", {}).get("session_id", "")
                if self.native_session_required and session_id != self.session_id:
                    self._calibration_off()
                    self.armed = False
                    self._lease_deadline = 0.0
                    if self._motion_task:
                        self._motion_task.cancel()
                        await asyncio.gather(self._motion_task, return_exceptions=True)
                self.session_id = session_id
                await self._hello()
                continue
            if self.native_session_required and message.get("session_id") != self.session_id:
                await self._reply(command_id, "command.ack", "rejected", error={"code": "session_required"})
                continue
            if message.get("v") != 2 or message.get("target_boot_id") != self.boot_id:
                await self._reply(command_id, "command.ack", "rejected", error={"code": "boot_id_mismatch"})
                continue
            if message.get("type") == "session.keepalive":
                if self.armed:
                    self._lease_deadline = asyncio.get_running_loop().time() + self.lease_timeout
                continue
            if command_id in self.cache:
                original, response = self.cache[command_id]
                if original == wire:
                    await self.transport.write_line(response)
                else:
                    await self._reply(command_id, "command.ack", "rejected", error={"code": "command_id_conflict"})
                continue
            msg_type = message.get("type")
            if self.gait_testing and msg_type in {'motion.test_plan','motion.test_run','diagnostic.get'}:
                from .gait_debug import GAITS,EXTRA_LIMITS,validate_plan,validate_drop_ramp,validate_turn_profile
                payload=message.get('payload',{})
                action=payload.get('action','walk_forward')
                if action not in GAITS:
                    reply=await self._reply(command_id,'command.ack','rejected',error={'code':'invalid_action'})
                    self.cache[command_id]=(wire,reply);continue
                baseline=GAITS[action]
                metadata={'zeros':[10,0,-10,2],'directions':[1,-1,1,-1],'limits':[[-90,90]]*4,
                          'baseline':baseline,'phase_ms':300,'action':action,'extra_limits':[[-45,45]]*4,'free_angles':True}
                if msg_type=='motion.test_run':
                    frames=payload.get('frames')
                    try:
                        if payload.get('confirm') is not True or not self.armed or self.estop_latched or self.active_id or self.calibration['active']:
                            raise ValueError('unsafe')
                        draft=[row[:] for row in baseline]
                        if not isinstance(frames,list):raise ValueError('invalid')
                        draft[:len(frames)]=frames
                        validate_plan(metadata,draft,list(range(1,len(frames)+1)))
                        validate_drop_ramp(action,frames,payload.get('drop_ramp',False))
                        validate_turn_profile(action,frames,payload.get('smooth',0),payload.get('settle',0))
                    except (ValueError,TypeError):
                        reply=await self._reply(command_id,'command.ack','rejected',error={'code':'invalid_or_unsafe_test'})
                        self.cache[command_id]=(wire,reply);continue
                    ack=await self._reply(command_id,'command.ack','accepted')
                    self.cache[command_id]=(wire,ack);self.active_id=command_id;self.execution_count+=1
                    self._motion_task=asyncio.create_task(self._finish_gait_test(command_id,wire,frames,action))
                    continue
                if msg_type=='motion.test_plan':
                    view=payload.get('view')
                    if view=='calibration':result={'zeros':metadata['zeros'],'directions':metadata['directions'],'limits':metadata['limits'],'extra_limits':[[-45,45]]*4,'hold_ms':300,'max_phases':12,'drop_ramp':True,'free_angles':True,'turn_smooth':True}
                    elif view=='frames':result={'frames':baseline,'hold_ms':300,'max_phases':12}
                    else:result=None
                else:
                    view=payload.get('view','boot')
                    result=({'view':'boot','reset_reason':0,'reset_name':'MOCK','previous_valid':False,'uptime_ms':0}
                            if view=='boot' else {'view':'previous','valid':False} if view=='previous' else {'view':'current',**self.gait_trace} if view=='current' else None)
                if result is None:
                    reply=await self._reply(command_id,'command.ack','rejected',error={'code':'invalid_payload'})
                else:
                    await self._reply(command_id,'command.ack','accepted')
                    reply=await self._reply(command_id,'command.completed','completed',payload={'result':result})
                self.cache[command_id]=(wire,reply);continue
            if msg_type.startswith("calibration."):
                self._calibration_expire()
                p = message.get("payload", {})
                valid = True
                lease = p.get("calibration_id")
                if msg_type == "calibration.begin":
                    servo = p.get("servo")
                    valid = (p.get("confirm") is True and type(servo) is int and 1 <= servo <= 4
                             and isinstance(lease, str) and 0 < len(lease) < 40
                             and not self.estop_latched and not self.armed and not self.active_id
                             and not self.calibration["active"])
                    if valid:
                        self.calibration.update(active=True, timed_out=False, servo=servo, angle_deg=0, pulse_us=1500)
                        self.calibration_id = lease
                        self.calibration_deadline = asyncio.get_running_loop().time() + 1.2
                        self.outputs = [i == servo-1 for i in range(4)]
                        self.calibration_writes += 1
                elif msg_type in {"calibration.set", "calibration.renew"}:
                    valid = self.calibration["active"] and lease == self.calibration_id
                    if msg_type == "calibration.set":
                        angle = p.get("angle_deg")
                        valid = valid and type(angle) is int and -20 <= angle <= 20
                        if valid:
                            self.calibration.update(angle_deg=angle, pulse_us=500+(angle+90)*2000//180)
                            self.calibration_writes += 1
                    elif valid:
                        self.calibration_deadline = asyncio.get_running_loop().time() + 1.2
                elif msg_type == "calibration.end":
                    self._calibration_off()
                elif msg_type != "calibration.status":
                    valid = False
                if not valid:
                    reply = await self._reply(command_id, "command.ack", "rejected", error={"code": "calibration_denied"})
                else:
                    await self._reply(command_id, "command.ack", "accepted")
                    self.calibration["estop"] = self.estop_latched
                    reply = await self._reply(command_id, "command.completed", "completed", payload={"result": self.calibration.copy()})
                self.cache[command_id] = (wire, reply)
                while len(self.cache) > 32:
                    self.cache.popitem(last=False)
                continue
            if msg_type == "motion.execute":
                if self.estop_latched:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "estop_latched"})
                    self.cache[command_id] = (wire, rejected); continue
                if not self.armed:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "disarmed"})
                    self.cache[command_id] = (wire, rejected); continue
                if self.active_id:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "busy"})
                    self.cache[command_id] = (wire, rejected); continue
                ack = await self._reply(command_id, "command.ack", "accepted")
                self.cache[command_id] = (wire, ack)
                self.execution_count += 1
                self.active_id = command_id
                self._motion_task = asyncio.create_task(self._finish_motion(command_id, wire))
                continue
            if msg_type == "dialogue.append":
                payload = message.get("payload", {})
                role = payload.get("role")
                text = payload.get("text")
                invalid = (role not in {"user", "tars"} or not isinstance(text, str) or not text or
                           len(text.encode("utf-8")) > 300 or
                           any(ord(char) <= 0x1f or 0x7f <= ord(char) <= 0x9f for char in text))
                if invalid:
                    rejected = await self._reply(command_id, "command.ack", "rejected",
                                                 error={"code": "invalid_payload"})
                    self.cache[command_id] = (wire, rejected)
                    continue
                self.dialogue.append((role, text))
                ack = await self._reply(command_id, "command.ack", "accepted")
                self.cache[command_id] = (wire, ack)
                terminal = await self._reply(command_id, "command.completed", "completed")
                self.cache[command_id] = (wire, terminal)
                continue
            if msg_type == "interaction.set":
                state = message.get("payload", {}).get("state")
                if state not in {"IDLE", "LISTENING", "THINKING", "SPEAKING"}:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "invalid_payload"})
                    self.cache[command_id] = (wire, rejected); continue
                self.interaction_state = state
                self.interaction_history.append(state)
                await self._reply(command_id, "command.ack", "accepted")
                terminal = await self._reply(command_id, "command.completed", "completed")
                self.cache[command_id] = (wire, terminal)
                continue
            if msg_type == "motion.cancel":
                target = message.get("payload", {}).get("target_command_id")
                if target != self.active_id or not self._motion_task:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "target_not_found"})
                    self.cache[command_id] = (wire, rejected); continue
                ack = await self._reply(command_id, "command.ack", "accepted")
                self.cache[command_id] = (wire, ack)
                self._motion_task.cancel()
                await asyncio.gather(self._motion_task, return_exceptions=True)
                terminal = await self._reply(command_id, "command.completed", "completed")
                self.cache[command_id] = (wire, terminal)
                continue
            ack = await self._reply(command_id, "command.ack", "accepted")
            self.cache[command_id] = (wire, ack)
            if msg_type == "safety.estop":
                self._calibration_off()
                self.estop_latched = True
                self.armed = False
                if self._motion_task:
                    self._motion_task.cancel()
                    await asyncio.gather(self._motion_task, return_exceptions=True)
                terminal = await self._reply(command_id, "command.completed", "completed")
            elif msg_type == "safety.clear":
                if self.active_id:
                    rejected = await self._reply(command_id, "command.ack", "rejected", error={"code": "busy"})
                    self.cache[command_id] = (wire, rejected); continue
                self.estop_latched = False
                terminal = await self._reply(command_id, "command.completed", "completed")
            elif msg_type == "safety.arm":
                if self.estop_latched or self.calibration["active"]:
                    terminal = await self._reply(command_id, "command.failed", "failed", error={"code": "estop_latched"})
                else:
                    self.armed = True
                    if self.gait_testing:self.outputs=[True]*4
                    self._lease_deadline = asyncio.get_running_loop().time() + self.lease_timeout
                    terminal = await self._reply(command_id, "command.completed", "completed")
            elif msg_type == "safety.disarm":
                self._calibration_off()
                self.armed = False
                self._lease_deadline = 0.0
                if self._motion_task:
                    self._motion_task.cancel()
                    await asyncio.gather(self._motion_task, return_exceptions=True)
                terminal = await self._reply(command_id, "command.completed", "completed")
            elif msg_type == "state.get":
                terminal = await self._reply(command_id, "command.completed", "completed", payload={"result": {
                    "sequence": self.execution_count,
                    "lifecycle": 1,
                    "interaction": ["IDLE", "LISTENING", "THINKING", "SPEAKING"].index(self.interaction_state),
                    "motion": 1 if self.active_id else 0,
                    "health": 2 if self.estop_latched else (0 if self.armed else 1),
                    "connected": True,
                    "armed": self.armed,
                    "active_command_id": self.active_id or "",
                }})
            else:
                terminal = await self._reply(command_id, "command.failed", "failed", error={"code": "unknown_type"})
            self.cache[command_id] = (wire, terminal)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)

    async def _lease_watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(0.05, self.lease_timeout / 2))
            self._calibration_expire()
            if self.armed and asyncio.get_running_loop().time() > self._lease_deadline:
                self.armed = False
                self._lease_deadline = 0.0
                if self._motion_task:
                    self._motion_task.cancel()
                    await asyncio.gather(self._motion_task, return_exceptions=True)

    def _calibration_off(self, timeout=False):
        self.calibration.update(active=False, timed_out=timeout, servo=0)
        self.outputs = [False] * 4
        self.calibration_id = ""

    def _calibration_expire(self):
        if self.calibration["active"] and asyncio.get_running_loop().time() >= self.calibration_deadline:
            self._calibration_off(True)

    async def _finish_gait_test(self,command_id,wire,frames,action='walk_forward'):
        terminal=None
        try:
            for phase,row in enumerate(frames,1):
                self.gait_test_history.append(row[:])
                self.gait_trace={'valid':True,'stage':'hold','action':{'walk_forward':0,'turn_left':1,'turn_right':2}[action],'cycle':1,'phase':phase,
                                 'mark_ms':phase*300,'command_id':command_id}
                await asyncio.sleep(self.motion_delay)
            self.gait_trace['stage']='complete'
            terminal=await self._reply(command_id,'command.completed','completed')
        except asyncio.CancelledError:
            self.gait_trace['stage']='cancelled'
            if not self._rebooting:terminal=await self._reply(command_id,'command.cancelled','cancelled')
        finally:
            self.armed=False;self.outputs=[False]*4;self.active_id=None;self._motion_task=None
            if terminal:self.cache[command_id]=(wire,terminal)

    async def _finish_motion(self, command_id: str, wire: bytes) -> None:
        terminal: bytes | None = None
        try:
            await asyncio.sleep(self.motion_delay)
            terminal = await self._reply(command_id, "command.completed", "completed")
        except asyncio.CancelledError:
            if not self._rebooting:
                terminal = await self._reply(command_id, "command.cancelled", "cancelled")
        finally:
            if terminal is not None:
                self.cache[command_id] = (wire, terminal)
            self.active_id = None
            self._motion_task = None
