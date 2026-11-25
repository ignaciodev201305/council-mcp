"""Grafo del consejo: evaluaciones a ciegas en paralelo y síntesis del supervisor."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from council.agents import ModelFactory, build_member, build_supervisor
from council.schemas import CouncilConfig
from council.settings import Settings

logger = logging.getLogger(__name__)

_TAGS = ("idea", "contexto", "evaluaciones")
_TAG_PATTERN = re.compile(rf"</?\s*({'|'.join(_TAGS)})\s*>", re.IGNORECASE)


class QuorumNotMetError(RuntimeError):
    """Indica que respondieron menos miembros que el quórum configurado."""


def _merge_reviews(old: dict | None, new: dict | None) -> dict:
    """Acumula reviews de una ejecución; None las reinicia al empezar otra."""
    return {} if new is None else {**(old or {}), **new}


def _merge_failed(old: list | None, new: list | None) -> list:
    """Acumula miembros fallidos de una ejecución; None los reinicia al empezar otra."""
    return [] if new is None else [*(old or []), *new]


class CouncilState(TypedDict):
    """Estado de una consulta al consejo dentro de la sesión."""

    idea: str
    project_context: str
    reviews: Annotated[dict[str, dict], _merge_reviews]
    failed: Annotated[list[str], _merge_failed]
    report: dict | None


def _wrap(tag: str, text: str) -> str:
    """Encierra texto no confiable en una etiqueta, quitando etiquetas que pudieran cerrarla antes."""
    return f"<{tag}>\n{_TAG_PATTERN.sub('', text)}\n</{tag}>"


def _member_message(state: CouncilState) -> str:
    """Arma el mensaje que recibe cada miembro."""
    context = state["project_context"] or "(sin contexto adicional)"
    return f"{_wrap('idea', state['idea'])}\n\n{_wrap('contexto', context)}"


def _supervisor_message(state: CouncilState) -> str:
    """Arma el mensaje del supervisor con las evaluaciones etiquetadas por miembro."""
    reviews = json.dumps(state["reviews"], ensure_ascii=False, indent=1)
    failed = f"\n\nNo respondieron: {', '.join(state['failed'])}." if state["failed"] else ""
    return f"{_member_message(state)}\n\n{_wrap('evaluaciones', reviews)}{failed}"


def _member_node(
    name: str, agent: CompiledStateGraph, timeout_s: float
) -> Callable[[CouncilState], Awaitable[dict]]:
    """Devuelve el nodo de un miembro; un fallo del miembro no corta la consulta."""

    async def run(state: CouncilState) -> dict:
        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [HumanMessage(_member_message(state))]}), timeout_s
            )
            review = result["structured_response"]
        except Exception as exc:  # El modelo es externo: cualquier error cuenta como miembro caído.
            logger.warning("El miembro %s falló: %s: %.300s", name, type(exc).__name__, exc)
            return {"failed": [name]}
        return {"reviews": {name: review.model_dump()}}

    return run


def _synthesize_node(
    supervisor: CompiledStateGraph, quorum: int, timeout_s: float
) -> Callable[[CouncilState], Awaitable[dict]]:
    """Devuelve el nodo del supervisor, que exige quórum antes de sintetizar."""

    async def run(state: CouncilState) -> dict:
        if len(state["reviews"]) < quorum:
            raise QuorumNotMetError(
                f"Respondieron {len(state['reviews'])} miembros y el quórum es {quorum}. "
                f"Fallaron: {', '.join(state['failed']) or 'ninguno'}."
            )
        result = await asyncio.wait_for(
            supervisor.ainvoke({"messages": [HumanMessage(_supervisor_message(state))]}), timeout_s
        )
        return {"report": result["structured_response"].model_dump()}

    return run


def build_council_graph(
    config: CouncilConfig, make_model: ModelFactory, settings: Settings, checkpointer: Checkpointer
) -> CompiledStateGraph:
    """Compila el grafo: START -> miembros en paralelo -> synthesize -> END."""
    builder = StateGraph(CouncilState)
    member_nodes = []
    for member in config.members:
        node = f"member_{member.name}"
        agent = build_member(member, config, make_model, settings)
        builder.add_node(node, _member_node(member.name, agent, settings.council_member_timeout_s))
        builder.add_edge(START, node)
        member_nodes.append(node)
    supervisor = build_supervisor(config, make_model, settings)
    builder.add_node("synthesize", _synthesize_node(supervisor, config.quorum, settings.council_member_timeout_s))
    builder.add_edge(member_nodes, "synthesize")
    builder.add_edge("synthesize", END)
    return builder.compile(checkpointer=checkpointer)


def council_serde() -> JsonPlusSerializer:
    """Devuelve el serializer de checkpoints con los schemas del consejo permitidos."""
    # Los agentes guardan su structured_response en el checkpoint; sin registrarlos, LangGraph los bloqueará.
    return JsonPlusSerializer(
        allowed_msgpack_modules=[("council.schemas", "MemberReview"), ("council.schemas", "CouncilReport")]
    )


def new_run_input(idea: str, project_context: str) -> dict:
    """Devuelve la entrada de una consulta nueva, reiniciando los resultados de la anterior."""
    return {"idea": idea, "project_context": project_context, "reviews": None, "failed": None, "report": None}
