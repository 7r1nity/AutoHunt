"""Current frontend transport: POST /runs, public state, SSE, resume button.

Uses the production handlers and runtime in an isolated ASGI app. Frontend React,
Next proxy, application startup/middleware and browser adapters are not exercised.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import gc
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import HTTPException, Query
from fastapi.responses import StreamingResponse

from app.runtime.models import RunCreate, public_run
from evaluation.pause_contract import ChatHarness, Scripted, MESSAGE, CONTINUE


class RunsPauseContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-pause-runs-")
        self.model = Scripted()
        self.h = ChatHarness(self.temp.name, self.model)
        source = Path(__file__).parents[1] / "app/api/application.py"
        names = {"create_run", "read_run", "run_events", "resume_run"}
        nodes = [n for n in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        self.handlers = {"app": self.h.app, "agent_runtime": self.h.runtime, "run_store": self.h.store,
            "HTTPException": HTTPException, "RunCreate": RunCreate, "public_run": public_run,
            "Query": Query, "StreamingResponse": StreamingResponse}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), self.handlers)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app), base_url="http://synthetic")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.h.close()
        self.h = None
        gc.collect()
        self.temp.cleanup()

    async def submit(self, message=MESSAGE, request_id="initial"):
        response = await self.client.post("/api/v1/runs", json={"conversation_id": "default",
            "request_id": request_id, "kind": "chat", "message": message})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        await self.h.runtime.wait_response(run_id)
        return run_id

    async def test_frontend_create_state_and_response_stream_are_paused(self):
        run_id = await self.submit()
        await self.h.wait_for(lambda: self.h.store.get(run_id)["status"] == "waiting_user")
        response = await self.client.get(f"/api/v1/runs/{run_id}")
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual(state["status"], "waiting_user")
        self.assertTrue(state["can_resume"])
        self.assertIsNone(state["browser_id"])
        self.assertIsNone(state["delivery_id"])
        self.assertFalse(state["mutation_started"])
        self.assertTrue(all(s["status"] == "pending" for s in state["plan_steps"]))
        self.assertEqual(state["approvals"], [])
        self.assertEqual(self.h.calls, [])
        # The paused run's SSE connection stays open. Consume exactly through the
        # response event (the frontend's settlement point), then disconnect.
        stream = await self.handlers["run_events"](run_id, after=0)
        seen = []
        try:
            async with asyncio.timeout(3):
                async for frame in stream.body_iterator:
                    if "\ndata: " not in frame:
                        continue
                    event = json.loads(frame.split("\ndata: ", 1)[1])
                    seen.append(event["kind"])
                    if event["kind"] == "response":
                        self.assertIn("暂停", event["data"]["result"]["message"])
                        break
        finally:
            await stream.body_iterator.aclose()
        self.assertIn("response", seen)
        self.assertNotIn("tool_started", seen)
        await asyncio.sleep(1.1)
        self.assertEqual(self.h.calls, [])  # Disconnecting the UI cannot resume work.

    async def test_duplicate_post_and_resume_button_start_original_search_once(self):
        run_id = await self.submit()
        before = copy.deepcopy(self.h.store.get(run_id)["checkpoint"]["plan"])
        self.assertEqual(await self.submit(), run_id)
        self.assertEqual(self.h.calls, [])
        for _ in range(2):
            response = await self.client.post(f"/api/v1/runs/{run_id}/resume")
            self.assertEqual(response.status_code, 200, response.text)
            await self.h.runtime.wait_response(response.json()["id"])
        await self.h.wait_for(lambda: bool(self.h.calls))
        self.assertEqual([c["tool"] for c in self.h.calls], ["job_search_start"])
        self.assertEqual(self.h.calls[0]["arguments"], before["steps"][0]["arguments"])
        self.assertFalse(self.h.store.get(run_id)["checkpoint"]["plan"]["paused"])

    async def test_frontend_natural_language_continue_keeps_same_plan(self):
        run_id = await self.submit()
        before = copy.deepcopy(self.h.store.get(run_id)["checkpoint"]["plan"])
        self.model.value = {"operation": "revise", "revision": {"pause": False, "updates": []}}
        await self.submit(CONTINUE, "continue")
        await self.h.wait_for(lambda: bool(self.h.calls))
        after = self.h.store.get(run_id)["checkpoint"]["plan"]
        self.assertEqual(after["id"], before["id"])
        self.assertFalse(after["paused"])
        self.assertEqual([c["tool"] for c in self.h.calls], ["job_search_start"])


if __name__ == "__main__":
    unittest.main()
