#!/usr/bin/env python3
"""Servidor MCP (Streamable HTTP, revisao 2026-07-28) da Central de Salas.

Construido sobre o SDK oficial `mcp` (v2). O transporte, a validacao dos
campos obrigatorios de `_meta`, o espelhamento dos headers Mcp-Method/
Mcp-Name, o ciclo de MRTR (`input_required`/`inputRequests`/`requestState`)
e a checagem da capability de elicitation em form mode (-32021) sao todos
resolvidos pelo framework -- nao ha protocolo reescrito na mao aqui.

O unico ponto de costura proprio e o resolver `escolha_de_sala`: ele decide,
por regra de negocio, se a reserva pode seguir direto ou se precisa
perguntar uma alternativa (`Elicit`), e o `reservar_sala` usa o resultado
para concluir ou recusar.

Suba com:

    python3 servidor-mcp/server.py

Variaveis de ambiente:

    MCP_HOST               padrao 127.0.0.1
    MCP_PORT               padrao 7301
    MCP_PATH               padrao /mcp
    REQUEST_STATE_SECRET   obrigatoria, >= 32 bytes de aleatoriedade
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import dominio
import uvicorn
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[mcp] %(message)s")
logger = logging.getLogger("central-de-salas")

REQUEST_STATE_TTL_SEGUNDOS = 15 * 60  # entre 5 e 30 minutos, como pede o enunciado


def _seguranca_do_request_state() -> RequestStateSecurity:
    segredo = os.environ.get("REQUEST_STATE_SECRET")
    if not segredo:
        raise RuntimeError(
            "REQUEST_STATE_SECRET nao esta definida. Gere uma com "
            "python3 -c \"import secrets; print(secrets.token_hex(32))\" "
            "e exporte antes de subir o servidor MCP."
        )
    return RequestStateSecurity(keys=[segredo], ttl=REQUEST_STATE_TTL_SEGUNDOS)


async def _log_de_requisicoes(ctx: Any, call_next: Any) -> Any:
    """Middleware exigido pelo enunciado: metodo, id e traceparent de cada request no stderr."""
    meta = ctx.meta or {}
    logger.info("method=%s id=%s traceparent=%s", ctx.method, ctx.request_id, meta.get("traceparent"))
    return await call_next(ctx)


DOMINIO = dominio.Dominio()

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=_seguranca_do_request_state(),
    middleware=[_log_de_requisicoes],
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> dict[str, list[dict[str, Any]]]:
    return {"salas": DOMINIO.salas}


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        inicio_dt, fim_dt = dominio.validar_pedido(DOMINIO, sala, inicio, fim)
    except dominio.ErroDeNegocio as exc:
        raise ToolError(str(exc)) from exc

    conflitos = DOMINIO.conflitos(sala, inicio_dt, fim_dt)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(id=c.id, inicio=c.inicio, fim=c.fim, responsavel=c.responsavel) for c in conflitos],
    )


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _criar_modelo_alternativas(alternativas: list[str]) -> type[BaseModel]:
    tipo = Literal[tuple(alternativas)]  # type: ignore[valid-type]
    return create_model("EscolhaDeSala", sala=(tipo, Field(description="Sala alternativa escolhida")))


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Any:
    """Resolver do MRTR: decide se a reserva segue direto ou precisa de elicitation.

    Roda antes do corpo de `reservar_sala`. Sem conflito, devolve a propria
    sala pedida (nenhuma pergunta e feita). Com conflito e sem alternativa,
    recusa com o erro de execucao. Com alternativas, devolve `Elicit(...)`;
    o framework decide, pela versao negociada, se isso vira um
    `InputRequiredResult` (>= 2026-07-28, o nosso caso) ou uma requisicao
    elicitation/create classica.
    """
    try:
        inicio_dt, fim_dt = dominio.validar_pedido(DOMINIO, sala, inicio, fim)
    except dominio.ErroDeNegocio as exc:
        raise ToolError(str(exc)) from exc

    if not DOMINIO.conflitos(sala, inicio_dt, fim_dt):
        return SimpleNamespace(sala=sala)

    alternativas = DOMINIO.alternativas(sala, inicio_dt, fim_dt)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)

    modelo = _criar_modelo_alternativas(alternativas)
    return Elicit(
        "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        modelo,
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if isinstance(escolha, DeclinedElicitation | CancelledElicitation):
        return ReservaOut(reservado=False, motivo="recusado")

    assert isinstance(escolha, AcceptedElicitation)
    sala_final = escolha.data.sala

    # arguments ja foram validados pelo resolver contra os mesmos sala/inicio/fim;
    # aqui so reconstruimos os datetimes para gravar a reserva.
    inicio_dt, fim_dt = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
    reserva = DOMINIO.criar_reserva(sala_final, inicio_dt, fim_dt, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        reservado=True,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=DOMINIO.politica_versao,
    )


@mcp.resource("politica://uso", mime_type="text/markdown", description="Politica de uso das salas.")
def politica_de_uso() -> str:
    return DOMINIO.politica_texto


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    caminho = os.environ.get("MCP_PATH", "/mcp")

    app = mcp.streamable_http_app(streamable_http_path=caminho, json_response=True, host=host)
    logger.info("servidor MCP em http://%s:%s%s", host, port, caminho)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
