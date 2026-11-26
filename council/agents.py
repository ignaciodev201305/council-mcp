"""Construcción de los agentes del consejo a partir de su configuración."""

from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, SummarizationMiddleware
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult
from langgraph.graph.state import CompiledStateGraph

from council.schemas import AgentConfig, CouncilConfig, CouncilReport, MemberConfig, MemberReview
from council.settings import Settings, read_prompt
from council.tools import TOOLS

ModelFactory = Callable[..., BaseChatModel]

# Medido: un miembro con tools usa 1-2 rondas de tools más 1-2 intentos de schema; con 4 fallaba ~25% de las veces.
_MAX_MODEL_CALLS_PER_RUN = 6
# Exa cobra un precio fijo por request (unos 0,007 USD); la búsqueda nativa del proveedor varía por modelo.
_WEB_SEARCH_PLUGIN = {"id": "web", "engine": "exa", "max_results": 5}


class CostTracker(AsyncCallbackHandler):
    """Suma el costo en dólares que OpenRouter informa en cada respuesta del modelo."""

    def __init__(self) -> None:
        self.cost_usd = 0.0

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = message.response_metadata.get("token_usage") if message else None
                self.cost_usd += float((usage or {}).get("cost") or 0.0)


def openrouter_model_factory(settings: Settings) -> ModelFactory:
    """Devuelve una función que crea modelos de OpenRouter con la clave del entorno."""
    if settings.openrouter_api_key is None:
        raise RuntimeError("Falta OPENROUTER_API_KEY en el entorno.")

    def make(model: str, temperature: float, web_search: bool = False) -> BaseChatModel:
        # Sin esto OpenRouter puede rutear a un proveedor lento; la latencia del mismo modelo variaba 16-130 s.
        extra_body: dict[str, Any] = {"provider": {"sort": "throughput"}}
        if web_search:
            extra_body["plugins"] = [_WEB_SEARCH_PLUGIN]
        # OpenRouter expone Chat Completions, no la Responses API de OpenAI.
        return init_chat_model(
            f"openai:{model}",
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key.get_secret_value(),
            temperature=temperature,
            use_responses_api=False,
            timeout=settings.council_model_timeout_s,
            max_retries=2,
            # Sin tope OpenRouter reserva crédito para el máximo del modelo (p. ej. 65k tokens) y responde 402 con saldo bajo.
            max_tokens=settings.council_max_output_tokens,
            extra_body=extra_body,
        )

    return make


def _build_agent(
    name: str,
    cfg: AgentConfig,
    system_prompt: str,
    tools: list,
    schema: type,
    config: CouncilConfig,
    make_model: ModelFactory,
    settings: Settings,
) -> CompiledStateGraph:
    """Crea un agente con salida estructurada y memoria resumida."""
    summarizer = SummarizationMiddleware(
        make_model(config.summarizer_model, 0.0),
        trigger=("tokens", settings.council_summary_trigger_tokens),
        keep=("messages", cfg.memory_window),
    )
    # ToolStrategy reintenta los errores de schema sin contador; el tope evita que un modelo que nunca cumple repita hasta el timeout.
    call_limit = ModelCallLimitMiddleware(run_limit=_MAX_MODEL_CALLS_PER_RUN, exit_behavior="error")
    # checkpointer=True guarda la memoria del agente en el hilo de la sesión, bajo su propio namespace.
    # Solo los miembros pueden buscar en la web; el supervisor sintetiza lo que ellos encontraron.
    web_search = isinstance(cfg, MemberConfig) and cfg.web_search
    return create_agent(
        make_model(cfg.model, cfg.temperature, web_search=web_search),
        tools=tools,
        system_prompt=system_prompt,
        response_format=ToolStrategy(schema) if cfg.structured_output == "tool" else ProviderStrategy(schema),
        middleware=[summarizer, call_limit],
        checkpointer=True,
        name=name,
    )


def build_member(
    member: MemberConfig, config: CouncilConfig, make_model: ModelFactory, settings: Settings
) -> CompiledStateGraph:
    """Crea el agente de un miembro con su lente y sus tools."""
    prompt = f"{read_prompt(config.member_base_prompt)}\n\n{read_prompt(member.prompt)}"
    tools = [TOOLS[t] for t in member.tools]
    return _build_agent(member.name, member, prompt, tools, MemberReview, config, make_model, settings)


def build_supervisor(config: CouncilConfig, make_model: ModelFactory, settings: Settings) -> CompiledStateGraph:
    """Crea el agente supervisor que sintetiza las evaluaciones."""
    prompt = read_prompt(config.supervisor.prompt)
    return _build_agent("supervisor", config.supervisor, prompt, [], CouncilReport, config, make_model, settings)
