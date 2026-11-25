"""Tools disponibles para los miembros del consejo."""

import json
import re
from typing import Literal
from urllib.parse import quote

import httpx
from langchain.tools import tool

_PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,49}/)?[a-z0-9][a-z0-9._-]{0,99}$")
# El JSON de PyPI incluye todo el historial de releases; algunos paquetes superan los 3 MB.
_MAX_RESPONSE_BYTES = 10_000_000
_TIMEOUT_S = 10.0


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """Descarga un JSON con tope de tamaño; devuelve None si el recurso no existe."""
    async with client.stream("GET", url) as response:
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


TOOLS = {"package_info": package_info}
