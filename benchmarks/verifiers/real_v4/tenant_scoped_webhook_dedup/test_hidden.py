import pytest

from service_hub.webhooks.api import handle_event
from service_hub.webhooks.store import DeliveryStore


def _delivery(tenant_id="alpha", event_id="evt-1", payload=None):
    return {
        "tenant_id": tenant_id,
        "event_id": event_id,
        "payload": payload if payload is not None else {"value": 1},
    }


def test_event_ids_are_scoped_by_tenant():
    store = DeliveryStore()
    calls = []

    assert handle_event(store, _delivery("alpha"), calls.append) == "processed"
    assert handle_event(store, _delivery("beta"), calls.append) == "processed"
    assert calls == [{"value": 1}, {"value": 1}]


def test_successful_delivery_is_deduplicated():
    store = DeliveryStore()
    calls = []
    delivery = _delivery()

    assert handle_event(store, delivery, calls.append) == "processed"
    assert handle_event(store, delivery, calls.append) == "duplicate"
    assert calls == [{"value": 1}]


def test_handler_failure_does_not_mark_delivery_seen():
    store = DeliveryStore()
    delivery = _delivery()
    attempts = []

    def fail(payload):
        attempts.append(("failed", payload))
        raise RuntimeError("temporary downstream failure")

    with pytest.raises(RuntimeError, match="temporary downstream failure"):
        handle_event(store, delivery, fail)
    assert store.snapshot() == frozenset()

    assert handle_event(store, delivery, attempts.append) == "processed"
    assert attempts[-1] == {"value": 1}


@pytest.mark.parametrize("missing", ["tenant_id", "event_id"])
def test_missing_identity_is_rejected_without_side_effects(missing):
    store = DeliveryStore()
    delivery = _delivery()
    del delivery[missing]
    calls = []

    with pytest.raises(KeyError):
        handle_event(store, delivery, calls.append)
    assert calls == []
    assert store.snapshot() == frozenset()
