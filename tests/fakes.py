"""Modelos y configuraciones falsas para probar el consejo sin red."""

import asyncio
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from council.schemas import CouncilConfig

REVIEW = {
    "summary": "Idea razonable con riesgos acotados.",
    "strengths": ["Resuelve un problema real", "Alcance chico"],
    "weaknesses": [
        {"text": "Depende de modelos externos", "severity": "media", "basis": "contexto"},
        {"text": "Costo variable", "severity": "baja", "basis": "supuesto"},
    ],
    "risks": [{"text": "Prompt injection; mitigación: delimitar entradas", "severity": "media", "basis": "supuesto"}],
    "improvements": ["Empezar con un MVP de dos miembros"],
    "open_questions": [],
    "feasibility": "alta",
    "confidence": 0.7,
}

REPORT = {
    "verdict": "viable_con_cambios",
    "summary": "Vale la pena con ajustes.",
    "consensus": ["El problema es real"],
    "disagreements": [],
    "strengths": ["Alcance chico"],
    "weaknesses": ["Costo variable"],
    "key_improvements": ["Medir costo por consulta"],
    "checks_for_coding_agent": ["Revisar si existe un checkpointer"],
    "open_questions": [],
}


class FakeToolModel(BaseChatModel):
    """Modelo que siempre responde llamando al tool de salida estructurada indicado."""

    tool_name: str
    payload: dict
    fail: bool = False
    delay_s: float = 0.0
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-tool"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeToolModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        if self.fail:
            raise RuntimeError("modelo caído")
        call = {"name": self.tool_name, "args": self.payload, "id": f"call_{len(self.calls)}"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])

    async def _agenerate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self._generate(messages, stop, run_manager, **kwargs)


class FakeTextModel(BaseChatModel):
    """Modelo que responde con texto plano, uno por llamada; repite el último al agotarse."""

    replies: list[str]
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-text"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeTextModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def fake_config(failing: set[str] | None = None, quorum: int = 3) -> tuple[CouncilConfig, dict[str, FakeToolModel], Callable[[str, float], FakeToolModel]]:
    """Devuelve una configuración de 4 miembros, sus modelos falsos y la factory que los entrega."""
    failing = failing or set()
    names = ["tecnico", "producto", "riesgos", "constructor"]
    config = CouncilConfig.model_validate(
        {
            "member_base_prompt": "prompts/members/_base.md",
            "quorum": quorum,
            "summarizer_model": "fake/summarizer",
            "supervisor": {"model": "fake/supervisor", "prompt": "prompts/supervisor.md"},
            "members": [
                {"name": n, "model": f"fake/{n}", "prompt": f"prompts/members/{n}.md"} for n in names
            ],
        }
    )
    models = {f"fake/{n}": FakeToolModel(tool_name="MemberReview", payload=REVIEW, fail=n in failing) for n in names}
    models["fake/supervisor"] = FakeToolModel(tool_name="CouncilReport", payload=REPORT)
    models["fake/summarizer"] = FakeToolModel(tool_name="none", payload={})

    def make(model: str, temperature: float) -> FakeToolModel:
        return models[model]

    return config, models, make
