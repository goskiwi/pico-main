import weakref
from typing import Generic, TypeVar

import pytest
from pydantic import BaseModel


T = TypeVar("T")


def test_parameterized_generic_preserves_explicit_slots():
    class Model(BaseModel, Generic[T]):
        __slots__ = ()

    Specialized = Model[int]

    assert Specialized.__dict__["__slots__"] == ()
    with pytest.raises(TypeError):
        weakref.ref(Specialized())
