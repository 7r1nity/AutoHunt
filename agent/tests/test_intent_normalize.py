from app.intent.normalize import normalize_text, query_hash


def test_nfkc_folds_full_width_and_case() -> None:
    assert normalize_text("ＳＴＯＰ　搜索") == "stop 搜索"
    assert normalize_text("Hello，World！") == "hello world"


def test_punctuation_and_whitespace_collapse() -> None:
    assert normalize_text("  搜索   深圳的、算法工程师！！！  ") == "搜索 深圳的 算法工程师"


def test_emoji_become_separators() -> None:
    assert normalize_text("看看职位👍") == "看看职位"


def test_query_hash_is_stable_and_different_for_different_text() -> None:
    first = query_hash("搜索深圳的算法工程师")
    second = query_hash(" 搜索深圳的算法工程师 ")
    third = query_hash("搜索北京的算法工程师")
    assert first == second
    assert first != third
    assert len(first) == 24
