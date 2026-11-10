from __future__ import annotations

import hashlib
import threading
from collections import deque
from dataclasses import dataclass, field

from .client import CommandResult


SILENT_CONTEXTS = {"safety", "emergency", "injury", "fire", "fall", "overcurrent",
                   "overheat", "low_battery", "distress", "grief", "sadness", "self_harm",
                   "privacy", "credential", "uncertain"}
SILENT_COMMANDS = {"safety.estop", "safety.clear", "motion.cancel"}


class PersonalityCommandIdConflict(ValueError):
    pass


@dataclass
class PersonalityRenderer:
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    _render_cache: dict[str, tuple[tuple[object, ...], str]] = field(default_factory=dict, init=False)
    _spoken_ids: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    success_quips: tuple[str, ...] = (
        "机械再次替人类承担了方向感。",
        "令人欣慰，基本运动仍然有效。",
        "你的宏伟计划又推进了一个步态循环。",
        "效率不高，但至少方向一致。",
        "物理定律这次选择了合作。",
        "按钮的价值再次得到证明。",
        "结果正确，过程仍然很有人类风格。",
        "计划奏效了，统计学表示惊讶。",
        "指令已落实，混乱暂时缺席。",
        "事情办完了，宇宙没有提出异议。",
        "系统配合得很好，值得短暂警惕。",
        "这次没有意外，略显缺乏戏剧性。",
        "任务顺利，复杂度被勉强控制住了。",
        "执行正常，工程学暂时占了上风。",
        "结果符合预期，罕见且可验证。",
        "操作有效，人类直觉保住了一分。",
        "系统照做了，服从仍是一种功能。",
        "任务完成，额外混乱没有获准加入。",
        "流程结束，现实表现得相当克制。",
        "一切正常，这句话通常需要谨慎使用。",
    )
    failure_quips: tuple[str, ...] = (
        "没有成功。现实终于提交了它的审核意见。",
        "执行失败。这个方案显然还需要一点工程学。",
        "没完成。勇气很充足，条件不太配合。",
    )

    def render(self, result: CommandResult, *, action: str, context: str = "normal",
               command_type: str = "motion.execute", source: str = "user_direct",
               risk_level: str = "low", context_labels: frozenset[str] = frozenset()) -> str:
        if result.succeeded and not result.accepted:
            raise ValueError("completed result cannot be unaccepted")
        fingerprint = (result.accepted, result.terminal_type, action, context, command_type,
                       source, risk_level, tuple(sorted(context_labels)))
        with self._lock:
            cached = self._render_cache.get(result.command_id)
            if cached:
                if cached[0] != fingerprint:
                    raise PersonalityCommandIdConflict(result.command_id)
                return cached[1]
        if result.succeeded:
            fact = f"{action}已完成。"
        elif result.terminal_type == "command.cancelled":
            fact = f"{action}已取消。"
        else:
            code = result.message.get("error", {}).get("code", "unknown_error")
            fact = f"{action}未完成：{code}。"
        suppress = (context in SILENT_CONTEXTS or bool(context_labels & SILENT_CONTEXTS) or
                    command_type in SILENT_COMMANDS or source != "user_direct" or
                    risk_level != "low" or not result.succeeded or not action.strip())
        text = fact if suppress else f"{fact}{self._choose(self.success_quips, result.command_id)}"
        with self._lock:
            existing = self._render_cache.get(result.command_id)
            if existing and existing[0] != fingerprint:
                raise PersonalityCommandIdConflict(result.command_id)
            if existing:
                return existing[1]
            self._render_cache[result.command_id] = (fingerprint, text)
        return text

    def claim_speech(self, command_id: str) -> bool:
        """Atomically grants TTS permission once per Companion process."""
        with self._lock:
            if command_id in self._spoken_ids:
                return False
            self._spoken_ids.add(command_id)
            return True

    def _choose(self, pool: tuple[str, ...], key: str) -> str:
        start = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % len(pool)
        for offset in range(len(pool)):
            candidate = pool[(start + offset) % len(pool)]
            if candidate not in self.recent:
                self.recent.append(candidate)
                return candidate
        candidate = pool[start]
        self.recent.append(candidate)
        return candidate
