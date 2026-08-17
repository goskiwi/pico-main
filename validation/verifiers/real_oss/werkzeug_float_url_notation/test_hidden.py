from werkzeug.routing import Map, Rule


def test_float_url_converter_does_not_use_scientific_notation():
    url_map = Map([Rule("/<float:value>", endpoint="value")])
    adapter = url_map.bind("example.test")

    built = adapter.build("value", {"value": 0.00001})

    assert "e" not in built.lower()
    assert built == "/0.00001"
