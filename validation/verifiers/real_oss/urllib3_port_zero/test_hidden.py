from urllib3 import PoolManager
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.util.url import parse_url


def test_explicit_port_zero_stays_distinct_from_an_absent_port():
    assert parse_url("http://example.test:0/path").netloc == "example.test:0"

    pool = PoolManager().connection_from_host("example.test", port=0, scheme="http")
    assert pool.port == 0

    with HTTPConnectionPool("example.test", port=0) as connection_pool:
        assert connection_pool.is_same_host("http://example.test:0/")
        assert not connection_pool.is_same_host("http://example.test/")
