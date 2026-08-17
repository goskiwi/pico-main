from jinja2 import Environment


def test_overlay_preserves_async_parent_unless_explicitly_overridden():
    async_environment = Environment(enable_async=True)

    assert async_environment.overlay().is_async
    assert not async_environment.overlay(enable_async=False).is_async
