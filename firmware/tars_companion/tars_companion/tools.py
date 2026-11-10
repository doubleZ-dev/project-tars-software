from __future__ import annotations

from .client import CommandResult, TarsClient
from .intent import MotionIntent, validate_motion


class ToolPolicyRejected(RuntimeError):
    pass


class BrainToolExecutor:
    """Narrow deterministic tool surface. It intentionally exposes no raw command()."""

    def __init__(self, client: TarsClient):
        self._client = client

    async def state(self) -> CommandResult:
        return await self._client.state()

    async def motion(self, intent: MotionIntent) -> CommandResult:
        intent = validate_motion(intent)
        snapshot_result = await self._client.state()
        if not snapshot_result.succeeded:
            raise ToolPolicyRejected("state_unavailable")
        snapshot = snapshot_result.message.get("payload", {}).get("result", {})
        allowed = (snapshot.get("lifecycle") == 1 and snapshot.get("motion") == 0 and
                   snapshot.get("health") == 0 and snapshot.get("connected") is True and
                   snapshot.get("armed") is True and not snapshot.get("active_command_id"))
        if not allowed:
            raise ToolPolicyRejected("motion_guard_rejected")
        return await self._client.execute(intent.action, steps=intent.steps, speed_pct=intent.speed_pct)

    async def estop(self) -> CommandResult:
        return await self._client.estop()

    async def clear_estop_local_operator(self) -> CommandResult:
        return await self._client.clear_estop()
