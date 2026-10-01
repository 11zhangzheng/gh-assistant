from retry_policy import retry_delay


def test_lowercase_header_is_respected():
    assert retry_delay({"retry-after": "7"}, default=1) == 7


def test_zero_and_invalid_values():
    assert retry_delay({"Retry-After": "0"}, default=1) == 0
    assert retry_delay({"Retry-After": "invalid"}, default=3) == 3
