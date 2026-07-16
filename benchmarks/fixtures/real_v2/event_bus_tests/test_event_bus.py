from event_bus import EventBus


def test_publish_returns_callback_results():
    bus = EventBus()
    bus.subscribe("created", lambda payload: payload["id"])
    assert bus.publish("created", {"id": 7}) == [7]
