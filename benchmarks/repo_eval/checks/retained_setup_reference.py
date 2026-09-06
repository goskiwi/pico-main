import logging
import pytest

@pytest.fixture
def setup_records(caplog):
    logging.warning("setup message")
    return caplog.get_records("setup")

def test_retained_setup_records_stay_in_the_setup_phase(caplog, setup_records):
    assert [record.getMessage() for record in setup_records] == ["setup message"]
    logging.warning("call message")
    assert [record.getMessage() for record in setup_records] == ["setup message"]
    assert [record.getMessage() for record in caplog.get_records("setup")] == ["setup message"]
