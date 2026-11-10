from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MotionIntent:
    action: str
    steps: int = 1
    speed_pct: int = 30


@dataclass(frozen=True)
class ControlIntent:
    action: str


@dataclass(frozen=True)
class ChatIntent:
    text: str


Intent = MotionIntent | ControlIntent | ChatIntent

_PRECISE_WAKE_CONTROL_ACTIONS = frozenset({"estop", "clear_estop"})

_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
               "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
               "十八": 18, "十九": 19, "二十": 20}


def _quantity(text: str, default: int = 1) -> int:
    for word, count in {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}.items():
        if re.search(r'\b' + word + r'\b', text):
            return count
    match = re.search(r"(?<![-\d])(\d{1,3})(?:\s*步|\s*次)?", text)
    if match:
        return int(match.group(1))
    chinese = re.search(r'[一二两三四五六七八九十]+', text)
    if chinese:
        return _CN_NUMBERS.get(chinese[0], 999)
    return default


def parse_intent(text: str) -> Intent:
    raw = text.strip()
    lowered = raw.lower().rstrip("。.!！?？")
    if not raw:
        return ChatIntent(raw)
    if re.search(r"(?:舵机|servo).*(?:角度|angle)|(?:角度|angle).*(?:舵机|servo)", lowered):
        return ControlIntent("forbidden_raw_servo")
    if lowered in {"急停", "停下", "停止", "立即停止", "stop", "please stop", "estop", "emergency stop"}:
        return ControlIntent("estop")
    if re.fullmatch(r"(?:请|麻烦)?\s*(?:后退|向后走|往后走)(?:\s*[一二两三123]\s*(?:步|次)?)?", lowered) or \
       re.fullmatch(r"(?:please\s+)?(?:back|backward|backwards|reverse|move back(?:ward|wards)?)(?:\s+[123])?(?:\s+steps?)?", lowered):
        return ControlIntent("unsupported_backward")
    if lowered in {"解除急停", "clear", "clear estop"}:
        return ControlIntent("clear_estop")
    if lowered in {"状态", "查询状态", "state", "status"}:
        return ControlIntent("state")
    if re.fullmatch(r"(?:请|麻烦)?\s*(?:前进|向前走|往前走)(?:\s*(?:[0-9]{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:步|次)?)?", lowered) or \
       re.fullmatch(r"(?:please\s+)?(?:forward|move forward|walk forward)(?:\s+(?:[0-9]{1,3}|one|two|three|four|five|six|seven|eight|nine|ten))?(?:\s+steps?)?", lowered):
        return MotionIntent("walk_forward", _quantity(lowered))
    if re.fullmatch(r"(?:请|麻烦)?\s*(?:左转|向左转)(?:\s*[一二两三123]\s*(?:次)?)?", lowered) or \
       re.fullmatch(r"(?:please\s+)?turn left(?:\s+[123])?(?:\s+times?)?", lowered):
        return MotionIntent("turn_left", _quantity(lowered))
    if re.fullmatch(r"(?:请|麻烦)?\s*(?:右转|向右转)(?:\s*[一二两三123]\s*(?:次)?)?", lowered) or \
       re.fullmatch(r"(?:please\s+)?turn right(?:\s+[123])?(?:\s+times?)?", lowered):
        return MotionIntent("turn_right", _quantity(lowered))
    return ChatIntent(raw)


def requires_precise_wake(intent: Intent) -> bool:
    """Return whether a voice intent may move hardware or change a safety latch."""
    return (isinstance(intent, MotionIntent) or
            isinstance(intent, ControlIntent) and
            intent.action in _PRECISE_WAKE_CONTROL_ACTIONS)


def validate_motion(intent: MotionIntent) -> MotionIntent:
    if intent.action not in {"walk_forward", "turn_left", "turn_right"}:
        raise ValueError("unsupported_action")
    if not 1 <= intent.steps <= (5 if intent.action == 'walk_forward' else 3):
        raise ValueError("steps_out_of_range")
    if not 10 <= intent.speed_pct <= 100:
        raise ValueError("speed_out_of_range")
    return intent
