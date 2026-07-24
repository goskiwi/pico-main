from copy import deepcopy

import pytest

from service_hub.auth.api import authorize
from service_hub.auth.service import effective_permissions


def _definitions():
    return {
        "viewer": {
            "permissions": ["documents:read"],
            "inherits": [],
        },
        "editor": {
            "permissions": ["documents:write"],
            "inherits": ["viewer"],
        },
        "publisher": {
            "permissions": ["documents:publish"],
            "inherits": ["editor"],
        },
        "auditor": {
            "permissions": ["audit:read", "documents:read"],
            "inherits": [],
        },
    }


def test_transitive_permissions_are_effective():
    definitions = _definitions()
    user = {"roles": ["publisher"]}
    assert authorize(user, "documents:publish", definitions) is True
    assert authorize(user, "documents:write", definitions) is True
    assert authorize(user, "documents:read", definitions) is True


def test_permissions_across_roles_are_deduplicated():
    permissions = effective_permissions(["publisher", "auditor"], _definitions())
    assert permissions == frozenset(
        {
            "documents:read",
            "documents:write",
            "documents:publish",
            "audit:read",
        }
    )


@pytest.mark.parametrize(
    "roles,definitions",
    [
        (["missing"], _definitions()),
        (
            ["editor"],
            {
                "editor": {
                    "permissions": ["documents:write"],
                    "inherits": ["missing"],
                }
            },
        ),
    ],
)
def test_unknown_direct_or_inherited_role_raises_key_error(roles, definitions):
    with pytest.raises(KeyError):
        effective_permissions(roles, definitions)


def test_inheritance_cycle_raises_documented_error():
    definitions = {
        "a": {"permissions": ["a:read"], "inherits": ["b"]},
        "b": {"permissions": ["b:read"], "inherits": ["c"]},
        "c": {"permissions": ["c:read"], "inherits": ["a"]},
    }
    with pytest.raises(ValueError, match="^role inheritance cycle$"):
        effective_permissions(["a"], definitions)


def test_resolution_does_not_mutate_inputs():
    definitions = _definitions()
    roles = ["publisher", "auditor"]
    original_definitions = deepcopy(definitions)
    original_roles = list(roles)
    effective_permissions(roles, definitions)
    assert definitions == original_definitions
    assert roles == original_roles
