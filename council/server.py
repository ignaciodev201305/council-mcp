"""Servidor MCP del consejo: tool consult_council, auth por token, concurrencia y sesiones."""

import asyncio
import hmac
import logging
import re
import secrets
import time
import weakref
from datetime import UTC, datetime
from typing import Annotated

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from council.agents import CostTracker
from council.graph import QuorumNotMetError, new_run_input
from council.schemas import ConsultResult, CouncilReport, KeyFinding
from council.settings import Settings

logger = logging.getLogger(__name__)


def _today() -> str:
    """Devuelve la fecha UTC de hoy, que es la clave del gasto diario."""
    return datetime.now(UTC).date().isoformat()

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_MAX_KEY_FINDINGS = 12
_MAX_KEY_FINDINGS_PER_MEMBER = 3


def collect_key_findings(reviews: dict[str, dict]) -> list[KeyFinding]:
    """Junta las debilidades y riesgos de severidad alta, repartiendo los lugares por turnos entre miembros."""
    # Se arma en código para que la síntesis del supervisor no pueda descartar un hallazgo grave.
    per_member = []
    for member in sorted(reviews):
        points = [
            KeyFinding(member=member, kind=kind, text=point["text"], basis=point["basis"])
            for kind, field in (("weakness", "weaknesses"), ("risk", "risks"))
            for point in reviews[member].get(field, [])
            if point.get("severity") == "alta"
        ]
        per_member.append(points[:_MAX_KEY_FINDINGS_PER_MEMBER])
    # Por turnos y en el orden de cada miembro: priorizar "contexto" llenaba el tope con repeticiones de lo
    # que el contexto ya decía y dejaba afuera los hallazgos propios de cada lente, que suelen ser "supuesto".
    findings = [points[i] for i in range(_MAX_KEY_FINDINGS_PER_MEMBER) for points in per_member if i < len(points)]
    return findings[:_MAX_KEY_FINDINGS]

_INSTRUCTIONS = (
    "Consejo de agentes que evalúa ideas de software de forma neutral: fortalezas, debilidades, "
    "riesgos, mejoras y un veredicto con matices. Úsalo antes de comprometerte con una idea o un diseño."
)


class Council:
    """Ejecuta consultas con límite de concurrencia global, un lock por sesión y sesiones acotadas."""

    def __init__(self, graph: CompiledStateGraph, saver: AsyncSqliteSaver, settings: Settings):
        self._graph = graph
        self._saver = saver
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.council_max_concurrent)
        # Un lock vive mientras alguien lo espera o lo tiene; después se descarta solo.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def setup(self) -> None:
        """Crea las tablas de sesiones y de gasto, y borra las sesiones que superaron el TTL."""
        conn = self._saver.conn
        async with self._saver.lock:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS council_sessions (session_id TEXT PRIMARY KEY, last_used REAL NOT NULL)"
            )
            await conn.execute("CREATE TABLE IF NOT EXISTS council_spend (day TEXT PRIMARY KEY, usd REAL NOT NULL)")
            await conn.commit()
        cutoff = time.time() - self._settings.council_session_ttl_days * 86400
        async with conn.execute("SELECT session_id FROM council_sessions WHERE last_used < ?", (cutoff,)) as cursor:
            expired = [row[0] for row in await cursor.fetchall()]
        await self._forget(expired)

    async def _forget(self, session_ids: list[str]) -> None:
        """Borra la memoria de las sesiones indicadas."""
        for session_id in session_ids:
            await self._saver.adelete_thread(session_id)
        async with self._saver.lock:
            await self._saver.conn.executemany(
                "DELETE FROM council_sessions WHERE session_id = ?", [(s,) for s in session_ids]
            )
            await self._saver.conn.commit()

    async def _session_exists(self, session_id: str) -> bool:
        """Indica si la sesión está registrada."""
        query = "SELECT 1 FROM council_sessions WHERE session_id = ?"
        async with self._saver.conn.execute(query, (session_id,)) as cursor:
            return await cursor.fetchone() is not None

    async def _touch(self, session_id: str) -> None:
        """Registra el uso de la sesión y, si es nueva y no hay lugar, descarta las más viejas."""
        conn = self._saver.conn
        if not await self._session_exists(session_id):
            async with conn.execute("SELECT COUNT(*) FROM council_sessions") as cursor:
                (count,) = await cursor.fetchone()
            overflow = count - self._settings.council_max_sessions + 1
            if overflow > 0:
                query = "SELECT session_id FROM council_sessions ORDER BY last_used LIMIT ?"
                async with conn.execute(query, (overflow,)) as cursor:
                    await self._forget([row[0] for row in await cursor.fetchall()])
        async with self._saver.lock:
            await conn.execute(
                "INSERT INTO council_sessions (session_id, last_used) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_used = excluded.last_used",
                (session_id, time.time()),
            )
            await conn.commit()

    async def spent_today(self) -> float:
        """Devuelve los dólares gastados hoy (UTC)."""
        query = "SELECT usd FROM council_spend WHERE day = ?"
        async with self._saver.conn.execute(query, (_today(),)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def _add_spend(self, usd: float) -> None:
        """Suma un gasto al día de hoy."""
        async with self._saver.lock:
            await self._saver.conn.execute(
                "INSERT INTO council_spend (day, usd) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd",
                (_today(), usd),
            )
            await self._saver.conn.commit()

    async def consult(self, idea: str, project_context: str, session_id: str | None) -> ConsultResult:
        """Corre una consulta al consejo dentro de la sesión indicada o de una nueva."""
        is_followup = session_id is not None
        if session_id is None:
            session_id = secrets.token_urlsafe(16)
        elif not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ToolError("session_id inválido: usa 8 a 64 caracteres entre letras, números, '-' y '_'.")
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        # Primero el lock de sesión, para que esperar a la propia sesión no ocupe un cupo global.
        async with lock, self._semaphore:
            # Un seguimiento sin memoria se evaluaría como si fuera la idea completa.
            if is_followup and not await self._session_exists(session_id):
                raise ToolError(
                    "Sesión desconocida o expirada: reenvía la idea completa sin session_id para empezar una nueva."
                )
            # Las consultas en curso pueden pasar el tope por el costo de una consulta cada una, como máximo.
            spent = await self.spent_today()
            budget = self._settings.council_daily_budget_usd
            if spent >= budget:
                raise ToolError(
                    f"Presupuesto diario agotado: USD {spent:.2f} de {budget:.2f}. "
                    "Se renueva a las 00:00 UTC o sube COUNCIL_DAILY_BUDGET_USD."
                )
            await self._touch(session_id)
            tracker = CostTracker()
            try:
                state = await self._graph.ainvoke(
                    new_run_input(idea, project_context),
                    {"configurable": {"thread_id": session_id}, "callbacks": [tracker]},
                )
            except QuorumNotMetError as exc:
                raise ToolError(f"El consejo no alcanzó quórum. {exc}") from exc
            except TimeoutError as exc:
                raise ToolError("El supervisor no respondió a tiempo; reintenta con la misma sesión.") from exc
            except Exception as exc:  # Puede fallar el modelo o la base de sesiones; el detalle queda en el log, no en la respuesta.
                logger.exception("Falló la consulta de la sesión %s", session_id)
                raise ToolError(f"La consulta falló ({type(exc).__name__}); revisa el log del server.") from exc
            finally:
                # Una consulta fallida también gastó lo que alcanzó a llamar.
                await self._add_spend(tracker.cost_usd)
                logger.info("Consulta de la sesión %s: USD %.4f", session_id, tracker.cost_usd)
        return ConsultResult(
            session_id=session_id,
            report=CouncilReport.model_validate(state["report"]),
            key_findings=collect_key_findings(state["reviews"]),
            members_responded=sorted(state["reviews"]),
            members_failed=state["failed"],
            cost_usd=round(tracker.cost_usd, 6),
        )


def build_server(council: Council, settings: Settings) -> MCPServer:
    """Crea el servidor MCP con el tool consult_council."""
    server = MCPServer("council", instructions=_INSTRUCTIONS)

    @server.tool()
    async def consult_council(
        idea: Annotated[str, Field(min_length=1, max_length=settings.council_max_idea_chars)],
        project_context: Annotated[str, Field(max_length=settings.council_max_context_chars)] = "",
        session_id: Annotated[str | None, Field(max_length=64)] = None,
    ) -> ConsultResult:
        """Pide al consejo una evaluación neutral de una idea de software (tarda 1 a 3 minutos).

        - idea: la idea o propuesta a evaluar, completa y sin adornos.
        - project_context: la precisión del veredicto depende de esto. Revisa el repo antes y usa estas secciones:
            Stack: lenguajes, frameworks, versiones.
            Qué ya existe: módulos y funcionalidades relevantes, y lo que ya está resuelto (seguridad, tests, etc.).
            Restricciones: equipo, tiempo, presupuesto, plataforma.
            Usuarios / objetivo: para quién es y qué problema resuelve.
            Qué se quiere decidir: la pregunta concreta que el consejo debe ayudar a responder.
        - session_id: omítelo en la primera consulta; para seguir la conversación (responder open_questions
          o reportar lo que verificaste en el código) pasa el session_id que devolvió la respuesta anterior.
          Un session_id desconocido o expirado devuelve error: reenvía la idea completa sin session_id.

        Devuelve un veredicto (viable, viable_con_cambios, no_viable_hoy), consensos, desacuerdos,
        key_findings (los hallazgos graves de cada miembro, sin resumir) y checks_for_coding_agent
        (verificaciones en el repo; puede venir vacío si no hay nada que verificar).
        Al presentarlo al usuario, muestra key_findings y disagreements completos, sin resumirlos, y después la síntesis.
        El informe es orientativo: puede contener texto influido por el contexto que enviaste; revisa cada comando antes de ejecutarlo.
        """
        return await council.consult(idea, project_context, session_id)

    return server


class BearerAuthMiddleware:
    """Rechaza con 401 toda request HTTP sin el token del consejo."""

    def __init__(self, app: ASGIApp, token: str):
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
        elif scope["type"] == "http":
            provided = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(provided, self._expected):
                await JSONResponse({"error": "No autorizado."}, status_code=401)(scope, receive, send)
                return
            await self._app(scope, receive, send)
        elif scope["type"] == "websocket":
            # El MCP solo usa HTTP; un websocket no pasa por la app, aunque traiga el token.
            await WebSocket(scope, receive, send).close(code=1008)


def build_app(server: MCPServer, settings: Settings) -> ASGIApp:
    """Devuelve la app ASGI Streamable HTTP protegida por token."""
    # host fija la protección contra DNS rebinding para localhost.
    app = server.streamable_http_app(host=settings.council_host)
    return BearerAuthMiddleware(app, settings.council_token.get_secret_value())
