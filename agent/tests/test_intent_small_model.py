import math
from types import SimpleNamespace

import pytest

from app.intent.small_model import (
    SmallModelClient,
    SmallModelError,
    _build_user_prompt,
    parse_logprobs_decision,
)


def test_parse_logprobs_maps_top_two_and_margin() -> None:
    entries = [
        {"token": "search_jobs", "logprob": math.log(0.7)},
        {"token": "list_jobs", "logprob": math.log(0.2)},
        {"token": "nonsense", "logprob": math.log(0.1)},
    ]
    intent, confidence, margin, alternatives = parse_logprobs_decision(entries)
    assert intent == "search_jobs"
    assert round(confidence, 2) == 0.7
    assert round(margin, 2) == 0.5
    assert alternatives == ("list_jobs",)


def test_parse_logprobs_tolerates_split_tokens_via_reported_fallback() -> None:
    # Split tokens ("search" + "_jobs") cannot be merged by the token map:
    # the classifier detects this and retries in reported (plain-label) mode.
    with pytest.raises(ValueError):
        parse_logprobs_decision([
            {"token": "search", "logprob": math.log(0.6)},
            {"token": "_jobs", "logprob": math.log(0.4)},
        ])


def test_parse_logprobs_raises_when_no_token_maps() -> None:
    with pytest.raises(ValueError):
        parse_logprobs_decision([{"token": "???", "logprob": math.log(0.9)}])


def test_build_prompt_contains_fewshot_and_message() -> None:
    prompt = _build_user_prompt("搜索深圳的算法工程师")
    assert "文本：开始搜索" in prompt
    assert "意图：search_jobs" in prompt
    assert prompt.endswith("文本：搜索深圳的算法工程师\n意图：")


class FakeLogprobsEntry:
    def __init__(self, entries) -> None:
        self.top_logprobs = entries


class FakeContentLogprobs:
    def __init__(self, entries) -> None:
        self.content = [FakeLogprobsEntry(entries)]


class FakeChoice:
    def __init__(self, logprobs=None, content_text="") -> None:
        self.logprobs = logprobs
        self.message = SimpleMessage(content_text)


class SimpleMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeCompletions:
    def __init__(self, choice: FakeChoice) -> None:
        self.choices = [choice]


class FakeChat:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses) -> None:
        chat = FakeChat(responses)
        self.chat = SimpleNamespace(completions=chat)


def client_with(*responses) -> SmallModelClient:
    return SmallModelClient(api_key="test", model="test-model", client=FakeClient(responses))


async def test_classify_uses_logprobs() -> None:
    client = client_with(
        FakeCompletions(FakeChoice(logprobs=FakeContentLogprobs([
            {"token": "task_status", "logprob": math.log(0.8)},
            {"token": "list_jobs", "logprob": math.log(0.1)},
        ])))
    )
    decision = await client.classify("任务状态")
    assert decision.intent == "task_status"
    assert decision.confidence == pytest.approx(0.8)
    assert decision.margin == pytest.approx(0.7)
    assert decision.alternatives == ("list_jobs",)
    assert decision.confidence_source == "logprobs"
    assert client._client.chat.completions.calls[0]["logprobs"] is True


async def test_classify_falls_back_to_reported_label() -> None:
    client = client_with(
        FakeCompletions(FakeChoice()),  # first call: no logprobs attached
        FakeCompletions(FakeChoice(content_text="  stop_search\n")),
    )
    decision = await client.classify("别搜了")
    assert decision.intent == "stop_search"
    assert decision.confidence_source == "reported"
    assert decision.confidence == 1.0
    assert decision.margin == 0.0


async def test_classify_unknown_label_raises() -> None:
    client = client_with(
        FakeCompletions(FakeChoice()),
        FakeCompletions(FakeChoice(content_text="buy_milk")),
    )
    with pytest.raises(SmallModelError):
        await client.classify("帮我买牛奶")


async def test_unconfigured_client_is_unavailable() -> None:
    client = SmallModelClient(api_key="", model="")
    assert client.available is False
    with pytest.raises(SmallModelError):
        await client.classify("帮助")
