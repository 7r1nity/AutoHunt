import asyncio
from types import SimpleNamespace as NS

import pytest

from app.llm.deepseek_provider import DeepSeekProvider


class Stream:
    def __init__(self, deltas):
        self.deltas = deltas
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for delta in self.deltas:
            yield NS(id="stream-one", choices=[NS(delta=NS(content=delta.get("text"),
                    reasoning_content=delta.get("reasoning"), tool_calls=delta.get("tools")))])

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_assembles_tool_arguments_and_redacts_split_private_text():
    provider = DeepSeekProvider(api_key="local-test-key", model="test-model")
    await provider.client.close()
    stream = Stream([
        {"text": "联系 secret@", "reasoning": "do not display this"},
        {"text": "example.com。"},
        {"tools": [NS(index=0, id="tool-one", function=NS(name="job_results_read", arguments='{"limit":'))]},
        {"tools": [NS(index=0, id=None, function=NS(name=None, arguments="3}"))]},
    ])
    async def create(**kwargs):
        assert kwargs["stream"] is True
        return stream
    provider.client = NS(chat=NS(completions=NS(create=create)))
    visible = []
    result = await provider.generate_with_tools_stream(instructions="instructions", messages=[], tools=[], on_delta=visible.append)
    assert "secret@example.com" not in "".join(visible)
    assert "do not display this" not in "".join(visible)
    assert result.tool_calls[0].arguments == '{"limit":3}'
    assert result.tool_calls[0].name == "job_results_read"
    assert stream.closed


@pytest.mark.asyncio
async def test_cancel_closes_provider_stream():
    provider = DeepSeekProvider(api_key="local-test-key", model="test-model")
    await provider.client.close()
    entered = asyncio.Event()
    class BlockingStream(Stream):
        async def iterate(self):
            entered.set()
            await asyncio.Event().wait()
            yield None
    stream = BlockingStream([])
    async def create(**_kwargs):
        return stream
    provider.client = NS(chat=NS(completions=NS(create=create)))
    task = asyncio.create_task(provider.generate_with_tools_stream(instructions="", messages=[], tools=[], on_delta=lambda _text: None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
