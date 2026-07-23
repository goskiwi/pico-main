from copy import deepcopy

import pytest

from service_hub.notifications.api import render_receipt


def _event(locale):
    return {"locale": locale, "values": {"total": "R$ 20", "name": "Ana"}}


def test_exact_locale_wins_over_base_and_default():
    templates = {
        "pt-BR": "BR {name}: {total}",
        "pt": "PT {name}: {total}",
        "default": "Default {name}: {total}",
    }
    assert render_receipt(_event("pt-BR"), templates) == "BR Ana: R$ 20"


def test_base_language_precedes_default():
    templates = {
        "pt": "PT {name}: {total}",
        "default": "Default {name}: {total}",
    }
    assert render_receipt(_event("pt-BR"), templates) == "PT Ana: R$ 20"


def test_default_is_used_after_exact_and_base_are_absent():
    templates = {"default": "Default {name}: {total}"}
    assert render_receipt(_event("ja-JP"), templates) == "Default Ana: R$ 20"


def test_missing_all_candidates_raises_key_error():
    with pytest.raises(KeyError):
        render_receipt(_event("ja-JP"), {"en": "Total: {total}"})


def test_rendering_does_not_mutate_inputs():
    event = _event("pt-BR")
    templates = {"pt": "PT {name}: {total}", "default": "Default {total}"}
    original_event = deepcopy(event)
    original_templates = deepcopy(templates)
    render_receipt(event, templates)
    assert event == original_event
    assert templates == original_templates
