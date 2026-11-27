"""Tests de los tools de los miembros con HTTP simulado."""

import json
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


def _vuln(vuln_id: str, published: str, fixed: str | None = None, severity: str | None = None) -> dict:
    """Arma una vulnerabilidad con el formato de OSV."""
    events = [{"introduced": "0"}, *([{"fixed": fixed}] if fixed else [])]
    vuln = {
        "id": vuln_id,
        "summary": f"Resumen de {vuln_id}",
        "published": published,
        "affected": [{"package": {"name": "requests", "ecosystem": "PyPI"}, "ranges": [{"events": events}]}],
    }
    if severity:
        vuln["database_specific"] = {"severity": severity}
    return vuln


class KnownVulnerabilitiesTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, handler, **args) -> str:
        with patch.object(tools.httpx, "AsyncClient", _client_with(handler)):
            return await tools.known_vulnerabilities.ainvoke({"ecosystem": "pypi", "name": "requests", **args})

    async def test_counts_open_and_lists_most_recent(self):
        def handler(request):
            self.assertEqual((request.method, request.url.host), ("POST", "api.osv.dev"))
            self.assertEqual(json.loads(request.content), {"package": {"name": "requests", "ecosystem": "PyPI"}})
            vulns = [_vuln(f"GHSA-{i}", f"2026-0{i}-01", fixed=None if i == 1 else f"2.{i}") for i in range(1, 8)]
            vulns[0]["database_specific"] = {"severity": "HIGH"}
            return httpx.Response(200, json={"vulns": vulns})

        result = await self._call(handler)
        self.assertIn("7 vulnerabilidades", result)
        self.assertIn("1 sin versión corregida", result)
        # Lista solo las más recientes, con su corrección.
        self.assertIn("GHSA-7 (sin clasificar, corregida en 2.7)", result)
        self.assertNotIn("GHSA-1 ", result)
        self.assertEqual(result.count("\n- "), tools._MAX_VULNS_LISTED)

    async def test_version_is_sent_and_no_vulns(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)["version"], "2.32.3")
            return httpx.Response(200, json={})

        result = await self._call(handler, version="2.32.3")
        self.assertIn("no registra vulnerabilidades", result)
        self.assertIn("2.32.3", result)

    async def test_paginated_result_says_at_least(self):
        body = {"vulns": [_vuln("GHSA-1", "2026-01-01", severity="CRITICAL")], "next_page_token": "x"}
        result = await self._call(lambda r: httpx.Response(200, json=body))
        self.assertIn("al menos 1", result)
        self.assertIn("CRITICAL", result)

    async def test_invalid_input_does_not_hit_network(self):
        def handler(request):
            raise AssertionError("no debería hacer requests")

        self.assertIn("inválido", await self._call(handler, name="../x"))
        self.assertIn("inválida", await self._call(handler, version="1.0; rm -rf"))

    async def test_server_error_is_reported(self):
        result = await self._call(lambda r: httpx.Response(503))
        self.assertIn("No se pudo consultar OSV", result)


if __name__ == "__main__":
    unittest.main()
