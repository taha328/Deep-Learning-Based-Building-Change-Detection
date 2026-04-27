from src.utils.hashing import build_request_hash


def test_request_hash_is_stable_for_sorted_payload() -> None:
    first = build_request_hash({"b": 2, "a": 1})
    second = build_request_hash({"a": 1, "b": 2})
    assert first == second
