"""Punto de entrada: python -m council levanta el servidor MCP HTTP."""

import asyncio
import logging

import aiosqlite
import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from council.agents import openrouter_model_factory
from council.graph import build_council_graph, council_serde
from council.server import Council, build_app, build_server
from council.settings import get_settings, load_council_config

_MIN_TOKEN_CHARS = 16


async def _main() -> None:
    """Valida la configuración, abre la memoria persistente y sirve el consejo."""
    settings = get_settings()
    token = settings.council_token.get_secret_value() if settings.council_token else ""
    if len(token) < _MIN_TOKEN_CHARS:
        raise SystemExit(f"Define COUNCIL_TOKEN en .env (mínimo {_MIN_TOKEN_CHARS} caracteres).")
    config = load_council_config(settings.council_config_path)
    make_model = openrouter_model_factory(settings)
    settings.council_db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(settings.council_db_path) as conn:
        saver = AsyncSqliteSaver(conn, serde=council_serde())
        await saver.setup()
        council = Council(build_council_graph(config, make_model, settings, saver), saver, settings)
        await council.setup()
        app = build_app(build_server(council, settings), settings)
        names = ", ".join(m.name for m in config.members)
        logging.getLogger(__name__).info("Consejo listo con %s (quórum %d).", names, config.quorum)
        await uvicorn.Server(
            uvicorn.Config(app, host=settings.council_host, port=settings.council_port, log_level="info")
        ).serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main())
