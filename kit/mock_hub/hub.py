"""A stand-in for the router's hub, in the same process as the bridge (no network, no account).

    hub = MockHub()
    bridge = hub.attach(bridge)                    # captures what the bridge sends the hub
    await hub.deliver(bridge, frames.owner("hi"))  # the hub's task.execute
    hub.replies                                    # task.complete results, by task id

It drives the bridge's own message handler, so everything core does (who the sender is, which
session, the untrusted label) runs as it does against the real router.
"""

from __future__ import annotations

import json
from typing import Any


class MockHub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []  # every (method, params) the bridge sent

    def attach(self, bridge: Any) -> Any:
        async def send_rpc(method: str, params: dict, msg_id: str | None = None) -> None:
            self.sent.append((method, params))

        bridge.send_rpc = send_rpc
        bridge.ws, bridge.registered = object(), True
        return bridge

    async def deliver(self, bridge: Any, task: dict) -> None:
        await bridge.handle_message(json.dumps({"jsonrpc": "2.0", "id": task["id"], "method": "task.execute",
                                                "params": task}))

    @property
    def replies(self) -> dict[str, dict]:
        return {p.get("task_id") or p.get("id"): p for m, p in self.sent if m == "task.complete"}
