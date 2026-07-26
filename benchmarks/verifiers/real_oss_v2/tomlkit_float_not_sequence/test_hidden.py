import ctypes

from tomlkit import parse


def test_parsed_float_is_not_a_cpython_sequence():
    value = parse("a = [1.0, 2.0, 3.0]")["a"][0]
    py_sequence_check = ctypes.pythonapi.PySequence_Check
    py_sequence_check.argtypes = [ctypes.py_object]
    py_sequence_check.restype = ctypes.c_int

    assert py_sequence_check(value) == 0
