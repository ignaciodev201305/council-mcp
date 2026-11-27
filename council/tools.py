"""Tools disponibles para los miembros del consejo."""

import json
import re
from typing import Literal
from urllib.parse import quote

import httpx
from langchain.tools import tool

_PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,49}/)?[a-z0-9][a-z0-9._-]{0,99}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}$")
_OSV_URL = "https://api.osv.dev/v1/query"
_OSV_ECOSYSTEMS = {"pypi": "PyPI", "npm": "npm"}
_MAX_VULNS_LISTED = 5
_MAX_SUMMARY_CHARS = 120
# El JSON de PyPI incluye todo el historial de releases; algunos paquetes superan los 3 MB.
_MAX_RESPONSE_BYTES = 10_000_000
_TIMEOUT_S = 10.0


async def _get_json(client: httpx.AsyncClient, url: str, body: dict | None = None) -> dict | None:
    """Descarga un JSON con tope de tamaño (POST si hay body); devuelve None si el recurso no existe."""
    async with client.stream("POST" if body else "GET", url, json=body) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("La respuesta del registro supera el tamaño permitido.")
    return json.loads(body)


async def _pypi_info(client: httpx.AsyncClient, name: str) -> str:
    """Resume la información de un paquete de PyPI."""
    data = await _get_json(client, f"https://pypi.org/pypi/{name}/json")
    if data is None:
        return f"El paquete '{name}' no existe en PyPI."
    info = data["info"]
    upload_times = [f["upload_time_iso_8601"] for f in data.get("urls", []) if f.get("upload_time_iso_8601")]
    license_ = info.get("license_expression") or (info.get("license") or "")[:80] or "no declarada"
    return (
        f"PyPI '{info['name']}': última versión {info['version']}, "
        f"publicada {min(upload_times) if upload_times else 'en fecha desconocida'}, "
        f"licencia {license_}, requiere Python {info.get('requires_python') or 'sin restricción'}."
    )


async def _npm_info(client: httpx.AsyncClient, name: str) -> str:
    """Resume la información de un paquete de npm."""
    latest = await _get_json(client, f"https://registry.npmjs.org/{quote(name, safe='@')}/latest")
    if latest is None:
        return f"El paquete '{name}' no existe en npm."
    search = await _get_json(client, f"https://registry.npmjs.org/-/v1/search?text={quote(name)}&size=5")
    date = next(
        (o["package"].get("date") for o in (search or {}).get("objects", []) if o["package"]["name"] == name),
        None,
    )
    return (
        f"npm '{name}': última versión {latest.get('version')}, "
        f"publicada {date or 'en fecha desconocida'}, licencia {latest.get('license') or 'no declarada'}."
    )


@tool
async def package_info(ecosystem: Literal["pypi", "npm"], name: str) -> str:
    """Consulta en PyPI o npm si un paquete existe, su última versión, cuándo se publicó y su licencia."""
    pattern = _PYPI_NAME if ecosystem == "pypi" else _NPM_NAME
    if not pattern.fullmatch(name):
        return f"Nombre de paquete inválido para {ecosystem}: {name!r}."
    try:
        # Las redirecciones solo las emite el propio registro (p. ej. al normalizar el nombre).
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True, max_redirects=3) as client:
            if ecosystem == "pypi":
                return await _pypi_info(client, name)
            return await _npm_info(client, name)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return f"No se pudo consultar {ecosystem} para '{name}': {type(exc).__name__}."


def _fixed_versions(vuln: dict, name: str) -> list[str]:
    """Devuelve las versiones que corrigen la vulnerabilidad para el paquete indicado."""
    return [
        event["fixed"]
        for affected in vuln.get("affected", [])
        if affected.get("package", {}).get("name", "").lower() == name.lower()
        for range_ in affected.get("ranges", [])
        for event in range_.get("events", [])
        if "fixed" in event
    ]


def _describe_vuln(vuln: dict, name: str) -> str:
    """Resume una vulnerabilidad en una línea: id, gravedad, corrección y resumen."""
    # Solo los avisos de GitHub traen la gravedad ya clasificada; el resto queda sin clasificar.
    severity = vuln.get("database_specific", {}).get("severity") or "sin clasificar"
    fixed = _fixed_versions(vuln, name)
    fix = f"corregida en {', '.join(fixed[:3])}" if fixed else "sin versión corregida"
    summary = (vuln.get("summary") or vuln.get("details") or "sin resumen").strip()[:_MAX_SUMMARY_CHARS]
    return f"- {vuln['id']} ({severity}, {fix}): {summary}"


@tool
async def known_vulnerabilities(ecosystem: Literal["pypi", "npm"], name: str, version: str | None = None) -> str:
    """Consulta en OSV las vulnerabilidades conocidas de un paquete de PyPI o npm, opcionalmente de una versión."""
    pattern = _PYPI_NAME if ecosystem == "pypi" else _NPM_NAME
    if not pattern.fullmatch(name):
        return f"Nombre de paquete inválido para {ecosystem}: {name!r}."
    if version is not None and not _VERSION.fullmatch(version):
        return f"Versión inválida: {version!r}."
    query: dict = {"package": {"name": name, "ecosystem": _OSV_ECOSYSTEMS[ecosystem]}}
    if version:
        query["version"] = version
    target = f"{ecosystem} '{name}'" + (f" {version}" if version else "")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            data = await _get_json(client, _OSV_URL, query) or {}
    except (httpx.HTTPError, ValueError) as exc:
        return f"No se pudo consultar OSV para {target}: {type(exc).__name__}."
    vulns = data.get("vulns", [])
    if not vulns:
        return f"OSV no registra vulnerabilidades conocidas para {target}."
    # Sin versión, OSV devuelve también las ya corregidas; contar las abiertas evita leer el historial como riesgo actual.
    open_count = sum(not _fixed_versions(v, name) for v in vulns)
    total = f"al menos {len(vulns)}" if data.get("next_page_token") else str(len(vulns))
    scope = "que afectan a esa versión" if version else f"en su historial, {open_count} sin versión corregida"
    recent = sorted(vulns, key=lambda v: v.get("published", ""), reverse=True)[:_MAX_VULNS_LISTED]
    lines = "\n".join(_describe_vuln(v, name) for v in recent)
    return f"OSV registra {total} vulnerabilidades para {target} {scope}. Las más recientes:\n{lines}"


TOOLS = {"package_info": package_info, "known_vulnerabilities": known_vulnerabilities}
