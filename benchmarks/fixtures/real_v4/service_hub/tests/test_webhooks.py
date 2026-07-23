from service_hub.webhooks.api import handle_event
from service_hub.webhooks.store import DeliveryStore


def test_duplicate_delivery_is_processed_once():
    store = DeliveryStore()
    calls = []
    delivery = {"tenant_id": "alpha", "event_id": "evt-1", "payload": {"ok": True}}

    assert handle_event(store, delivery, calls.append) == "processed"
    assert handle_event(store, delivery, calls.append) == "duplicate"
    assert calls == [{"ok": True}]
