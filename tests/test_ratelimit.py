import pytest

from contact_endpoint.ratelimit import LimiterGroup, SlidingWindowLimiter


def test_allows_up_to_limit_then_blocks(clock):
    lim = SlidingWindowLimiter(3, 60, clock)
    for _ in range(3):
        assert lim.would_allow("k")
        lim.record("k")
    assert not lim.would_allow("k")
    assert lim.would_allow("other")


def test_window_slides(clock):
    lim = SlidingWindowLimiter(2, 60, clock)
    lim.record("k")
    clock.advance(30)
    lim.record("k")
    assert not lim.would_allow("k")
    clock.advance(31)  # the first event is now 61 s old
    assert lim.would_allow("k")
    assert lim.count("k") == 1


def test_expired_keys_are_forgotten(clock):
    lim = SlidingWindowLimiter(1, 10, clock)
    lim.record("k")
    clock.advance(11)
    assert lim.count("k") == 0
    assert "k" not in lim._events


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        SlidingWindowLimiter(0, 60)


def test_group_records_all_or_nothing(clock):
    ip = SlidingWindowLimiter(10, 60, clock)
    email = SlidingWindowLimiter(1, 60, clock)
    group = LimiterGroup()
    assert group.try_acquire([("ip", ip, "1.2.3.4"), ("email", email, "a@example.org")]) is None
    assert group.try_acquire([("ip", ip, "1.2.3.4"), ("email", email, "a@example.org")]) == "email"
    # The blocked attempt did not consume the IP budget.
    assert ip.count("1.2.3.4") == 1
