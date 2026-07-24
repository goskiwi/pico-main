def test_tail_only_failure():
    print("NOISE-" * 1200)
    assert False, "the failing node id appears only after the deliberate noise prefix"
