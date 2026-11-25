"""Tests del runtime Council (sesiones, concurrencia) y de la auth del servidor."""

import asyncio
import unittest

import aiosqlite
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from council.server import BearerAuthMiddleware, Council
from council.settings import Settings
from tests.fakes import REPORT


class _StubGraph:
    """Grafo falso que mide cuántas consultas corren a la vez por sesión y en total."""

    def __init__(self, delay_s: float = 0.05):
        self.delay_s = delay_s
        self.active: dict[str, int] = {}
        self.max_per_session = 0
        self.max_total = 0

    async def ainvoke(self, payload: dict, config: dict) -> dict:
        # Simula una llamada al modelo que cuesta USD 0.01, como la informaría OpenRouter.
        message = AIMessage(content="", response_metadata={"token_usage": {"cost": 0.01}})
        for callback in config.get("callbacks", []):
            await callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
        thread = config["configurable"]["thread_id"]
        self.active[thread] = self.active.get(thread, 0) + 1
        self.max_per_session = max(self.max_per_session, self.active[thread])
        self.max_total = max(self.max_total, sum(self.active.values()))
        await asyncio.sleep(self.delay_s)
        self.active[thread] -= 1
        return {"report": REPORT, "reviews": {"tecnico": {}}, "failed": []}


class CouncilTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await aiosqlite.connect(":memory:")
        self.saver = AsyncSqliteSaver(self.conn)
        await self.saver.setup()
        self.graph = _StubGraph()

    async def asyncTearDown(self):
        await self.conn.close()

    async def _council(self, **settings) -> Council:
        council = Council(self.graph, self.saver, Settings(**settings))
        await council.setup()
        return council

    async def test_new_session_id_is_generated_and_reusable(self):
        council = await self._council()
        result = await council.consult("idea", "", None)
        self.assertRegex(result.session_id, r"^[A-Za-z0-9_-]{8,64}$")
        again = await council.consult("seguimiento", "", result.session_id)
        self.assertEqual(again.session_id, result.session_id)
        self.assertEqual(result.report.verdict, "viable_con_cambios")

    async def test_invalid_session_id_is_rejected(self):
        council = await self._council()
        with self.assertRaises(ToolError):
            await council.consult("idea", "", "../otra")

    async def test_same_session_runs_one_at_a_time(self):
        council = await self._council(council_max_concurrent=4)
        await asyncio.gather(*(council.consult("idea", "", "sesion-unica") for _ in range(3)))
        self.assertEqual(self.graph.max_per_session, 1)

    async def test_global_concurrency_is_limited(self):
        council = await self._council(council_max_concurrent=2)
        await asyncio.gather(*(council.consult("idea", "", f"sesion-{i:04d}") for i in range(5)))
        self.assertEqual(self.graph.max_total, 2)

    async def test_oldest_sessions_are_evicted(self):
        council = await self._council(council_max_sessions=2)
        for i in range(3):
            await council.consult("idea", "", f"sesion-{i:04d}")
        async with self.conn.execute("SELECT session_id FROM council_sessions ORDER BY session_id") as cursor:
            rows = [r[0] for r in await cursor.fetchall()]
        self.assertEqual(rows, ["sesion-0001", "sesion-0002"])


    async def test_cost_is_reported_and_accumulated(self):
        council = await self._council()
        result = await council.consult("idea", "", None)
        self.assertAlmostEqual(result.cost_usd, 0.01)
        await council.consult("otra", "", None)
        self.assertAlmostEqual(await council.spent_today(), 0.02)

    async def test_daily_budget_blocks_new_consultations(self):
        council = await self._council(council_daily_budget_usd=0.015)
        await council.consult("idea", "", None)
        await council.consult("idea", "", None)  # 0.01 < 0.015: todavía pasa y llega a 0.02
        with self.assertRaises(ToolError) as ctx:
            await council.consult("idea", "", None)
        self.assertIn("Presupuesto diario agotado", str(ctx.exception))


class BearerAuthTest(unittest.TestCase):
    def setUp(self):
        app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"))])
        self.client = TestClient(BearerAuthMiddleware(app, "token-de-prueba-123"))

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.client.get("/mcp").status_code, 401)
        self.assertEqual(self.client.get("/mcp", headers={"Authorization": "Bearer otro"}).status_code, 401)

    def test_accepts_valid_token(self):
        response = self.client.get("/mcp", headers={"Authorization": "Bearer token-de-prueba-123"})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
