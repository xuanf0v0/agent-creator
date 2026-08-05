from __future__ import annotations

import asyncio
from collections import deque


class LogBroker:
    def __init__(self, agent_ids: list[str], capacity: int = 2000) -> None:
        self._buffers = {agent_id: deque(maxlen=capacity) for agent_id in agent_ids}
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {
            agent_id: set() for agent_id in agent_ids
        }

    def publish(self, agent_id: str, line: str) -> None:
        self._buffers[agent_id].append(line)
        for queue in tuple(self._subscribers[agent_id]):
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def tail(self, agent_id: str, lines: int = 200) -> list[str]:
        values = list(self._buffers[agent_id])
        return values[-lines:] if lines > 0 else values

    def subscribe(self, agent_id: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers[agent_id].add(queue)
        return queue

    def unsubscribe(self, agent_id: str, queue: asyncio.Queue[str]) -> None:
        self._subscribers[agent_id].discard(queue)
