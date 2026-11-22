"""Provider formatting contracts; fake responses, zero external requests."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.llm.base import LLMRequest
from app.llm.deepseek_provider import DeepSeekProvider


class JsonModeContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = DeepSeekProvider(api_key='synthetic-only-not-a-real-key', model='synthetic-model')
        response = NS(choices=[NS(message=NS(content='{"status":"synthetic"}'))],
                      model='synthetic-model', id='synthetic-response', usage=None)
        self.provider._completion = AsyncMock(return_value=response)

    async def asyncTearDown(self):
        await self.provider.client.close()

    async def test_json_mode_always_supplies_transport_required_json_instruction(self):
        original = '只输出符合给定结构的对象，不调用工具。'
        request = LLMRequest(instructions=original, user_message='合成需求', context='合成上下文',
                             json_output=True, fast=True, max_output_tokens=900)
        await self.provider.generate(request)
        call = self.provider._completion.await_args.kwargs
        self.assertEqual(call['response_format'], {'type': 'json_object'})
        self.assertTrue(call['messages'][0]['content'].startswith(original))
        self.assertIn('one valid JSON object', call['messages'][0]['content'])
        self.assertEqual(call['max_tokens'], 900)
        self.assertEqual(self.provider._completion.await_count, 1)
        self.assertEqual(request.instructions, original)

    async def test_plain_text_mode_does_not_force_json_or_change_user_instructions(self):
        original = '请直接给出简短解释。'
        await self.provider.generate(LLMRequest(instructions=original, user_message='合成提问', context='', fast=True))
        call = self.provider._completion.await_args.kwargs
        self.assertNotIn('response_format', call)
        self.assertEqual(call['messages'][0]['content'], original)
        self.assertEqual(self.provider._completion.await_count, 1)

    async def test_json_mode_does_not_change_application_fields_or_add_format_retries(self):
        self.provider._completion.side_effect = RuntimeError('synthetic provider failure')
        with self.assertRaises(RuntimeError):
            await self.provider.generate(LLMRequest(instructions='仅选择证据。', user_message='{"job_id":7}',
                                                  context='不产生任何执行授权', json_output=True))
        call = self.provider._completion.await_args.kwargs
        self.assertIn('{"job_id":7}', call['messages'][1]['content'])
        self.assertNotIn('tools', call)
        self.assertEqual(self.provider._completion.await_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
