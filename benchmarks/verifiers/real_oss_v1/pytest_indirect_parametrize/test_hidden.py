def test_direct_override_does_not_duplicate_parametrization(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture(params=["a", "b"])
        def target(request):
            return request.param

        @pytest.fixture
        def value(request):
            return int(request.param)

        @pytest.mark.parametrize(
            ["value", "target"],
            [("1", 1), ("2", 2)],
            indirect=["value"],
        )
        def test_case(value, target):
            assert value == target
        """
    )

    result = pytester.runpytest()

    assert result.ret == 0
    result.assert_outcomes(passed=2)
