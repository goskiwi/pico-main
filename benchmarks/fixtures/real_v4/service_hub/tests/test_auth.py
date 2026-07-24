from service_hub.auth.api import authorize


def test_direct_role_permissions_are_checked():
    definitions = {
        "viewer": {"permissions": ["documents:read"], "inherits": []},
    }
    assert authorize({"roles": ["viewer"]}, "documents:read", definitions) is True
    assert authorize({"roles": ["viewer"]}, "documents:write", definitions) is False
