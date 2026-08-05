from agent_harness.logs import LogBroker


def test_log_tail_capacity_and_subscription() -> None:
    broker = LogBroker(["a"], capacity=3)
    queue = broker.subscribe("a")
    for value in ("1", "2", "3", "4"):
        broker.publish("a", value)
    assert broker.tail("a", 2) == ["3", "4"]
    assert [queue.get_nowait() for _ in range(4)] == ["1", "2", "3", "4"]
    broker.unsubscribe("a", queue)
