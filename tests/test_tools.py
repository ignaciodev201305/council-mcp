"""Tests del tool package_info con HTTP simulado."""

import unittest
from unittest.mock import patch

import httpx

from council import tools

_REAL_CLIENT = httpx.AsyncClient


def _client_with(handler):
    """Devuelve un reemplazo de AsyncClient que responde con el handler dado."""

    def factory(**kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


class PackageInfoTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, handler, ecosystem: str, name: str) -> str:
        with patch.object(tools.httpx, "AsyncClient", _client_with(handler)):
            return await tools.package_info.ainvoke({"ecosystem": ecosystem, "name": name})

    async def test_pypi_existing_package(self):
        def handler(request):
            self.assertEqual(request.url.host, "pypi.org")
            return httpx.Response(200, json={
                "info": {"name": "langgraph", "version": "1.2.0", "license_expression": "MIT", "requires_python": ">=3.10"},
                "urls": [{"upload_time_iso_8601": "2026-10-01T00:00:00Z"}],
            })

        result = await self._call(handler, "pypi", "langgraph")
        self.assertIn("1.2.0", result)
        self.assertIn("2026-10-01", result)
        self.assertIn("MIT", result)

    async def test_npm_missing_package(self):
        result = await self._call(lambda r: httpx.Response(404), "npm", "no-existe-xyz")
        self.assertIn("no existe en npm", result)

    async def test_invalid_name_does_not_hit_network(self):
        def handler(request):
            raise AssertionError("no debería hacer requests")

        result = await self._call(handler, "pypi", "../../etc/passwd")
        self.assertIn("inválido", result)

    async def test_oversized_response_is_rejected(self):
        with patch.object(tools, "_MAX_RESPONSE_BYTES", 10):
            result = await self._call(lambda r: httpx.Response(200, content=b"x" * 100), "pypi", "grande")
        self.assertIn("No se pudo consultar", result)

    async def test_server_error_is_reported(self):
        result = await self._call(lambda r: httpx.Response(500), "pypi", "roto")
        self.assertIn("HTTPStatusError", result)


if __name__ == "__main__":
    unittest.main()
