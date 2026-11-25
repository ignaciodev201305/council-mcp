"""Modelos Pydantic de configuración del consejo y de sus salidas estructuradas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolName = Literal["package_info"]
Feasibility = Literal["alta", "media", "baja"]
Verdict = Literal["viable", "viable_con_cambios", "no_viable_hoy"]
Severity = Literal["alta", "media", "baja"]
Basis = Literal["contexto", "supuesto"]


class AgentConfig(BaseModel):
    """Configuración de un agente del consejo (miembro o supervisor)."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, description="ID del modelo en OpenRouter.")
    temperature: float = Field(default=0.3, ge=0, le=2)
    prompt: str = Field(min_length=1, description="Ruta al prompt .md, relativa a la raíz del proyecto.")
    # El mínimo de 2 garantiza que el último intercambio nunca se resuma.
    memory_window: int = Field(default=2, ge=2, le=50)
    # "tool" reintenta si la salida no valida; "native" sirve a modelos que rechazan tool_choice forzado.
    structured_output: Literal["tool", "native"] = "tool"


class MemberConfig(AgentConfig):
    """Configuración de un miembro: agrega nombre y tools habilitadas."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    tools: list[ToolName] = Field(default_factory=list)


class CouncilConfig(BaseModel):
    """Contenido validado de council.yaml."""

    model_config = ConfigDict(extra="forbid")

    member_base_prompt: str = Field(min_length=1)
    quorum: int = Field(ge=1)
    summarizer_model: str = Field(min_length=1)
    supervisor: AgentConfig
    members: list[MemberConfig] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _check_members(self) -> "CouncilConfig":
        """Rechaza nombres repetidos y un quórum mayor que la cantidad de miembros."""
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError("Los nombres de los miembros deben ser únicos.")
        if self.quorum > len(self.members):
            raise ValueError("El quórum no puede ser mayor que la cantidad de miembros.")
        return self


class Point(BaseModel):
    """Crítica con su gravedad y si se apoya en el contexto o es una inferencia."""

    text: str
    severity: Severity = Field(description="alta: cambia el veredicto; media: requiere un cambio; baja: detalle.")
    basis: Basis = Field(description="contexto: se apoya en lo que dice el contexto; supuesto: lo infieres sin evidencia.")


class MemberReview(BaseModel):
    """Evaluación de la idea desde la lente de un miembro del consejo."""

    summary: str = Field(description="Evaluación general en 2 o 3 frases.")
    strengths: list[str] = Field(min_length=2, description="Fortalezas concretas y específicas de esta idea.")
    weaknesses: list[Point] = Field(min_length=2, description="Debilidades concretas y específicas de esta idea.")
    risks: list[Point] = Field(default_factory=list, description="Riesgos plausibles, cada uno con su mitigación.")
    improvements: list[str] = Field(min_length=1, description="Cambios concretos que mejorarían la idea.")
    open_questions: list[str] = Field(default_factory=list, description="Información que falta para juzgar mejor.")
    feasibility: Feasibility = Field(description="Factibilidad desde tu lente.")
    confidence: float = Field(ge=0, le=1, description="Cuánto sabes realmente para sostener esta evaluación.")


class Disagreement(BaseModel):
    """Punto en el que los miembros del consejo discrepan."""

    topic: str
    positions: str = Field(description="Qué sostiene cada miembro involucrado.")
    assessment: str = Field(description="Qué postura está mejor fundada y por qué.")


class CouncilReport(BaseModel):
    """Síntesis neutral del supervisor para el agente de coding."""

    verdict: Verdict
    summary: str = Field(description="Conclusión en 3 a 5 frases.")
    consensus: list[str] = Field(description="Puntos en los que los miembros coinciden.")
    disagreements: list[Disagreement] = Field(default_factory=list)
    strengths: list[str] = Field(min_length=1)
    weaknesses: list[str] = Field(min_length=1)
    key_improvements: list[str] = Field(description="Cambios más importantes, en orden de prioridad.")
    checks_for_coding_agent: list[str] = Field(
        # Sin max_length: con salida nativa no hay reintento, y un sexto check tumbaría la síntesis entera.
        min_length=1,
        description="De 1 a 5 verificaciones, cada una ligada a una debilidad o pregunta concreta de esta idea.",
    )
    open_questions: list[str] = Field(default_factory=list, description="Preguntas que más cambiarían el veredicto.")


class ConsultResult(BaseModel):
    """Respuesta del tool MCP consult_council."""

    session_id: str
    report: CouncilReport
    members_responded: list[str]
    members_failed: list[str]
    cost_usd: float = Field(description="Costo de esta consulta en dólares, según OpenRouter.")
