"""Parser do formato fixo de pedido. Sem LLM, sem linguagem natural: regra."""

from __future__ import annotations

import re

PADRAO_RESERVAR = re.compile(
    r"^reservar sala=(?P<sala>\S+) inicio=(?P<inicio>\S+) fim=(?P<fim>\S+) responsavel=(?P<responsavel>.+)$"
)
PADRAO_ESCOLHA = re.compile(r"^escolha=(?P<valor>.+)$")


def parse_reservar(texto: str) -> dict | None:
    m = PADRAO_RESERVAR.match(texto.strip())
    return m.groupdict() if m else None


def parse_escolha(texto: str) -> str | None:
    m = PADRAO_ESCOLHA.match(texto.strip())
    return m.group("valor") if m else None
