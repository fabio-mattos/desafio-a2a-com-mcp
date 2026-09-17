"""Dados e regras de negocio da Central de Salas.

Le dados/salas.json e dados/politica-de-uso.md (nunca escreve neles). As
reservas vivem em memoria, a partir da semente de dados/reservas.json.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeNegocio(Exception):
    """Erro de execucao da tool: vira isError=true com a mensagem exata."""


def _carregar_salas() -> list[dict]:
    with (DADOS / "salas.json").open(encoding="utf-8") as f:
        return json.load(f)


def _carregar_reservas_iniciais() -> list[dict]:
    with (DADOS / "reservas.json").open(encoding="utf-8") as f:
        return json.load(f)


def carregar_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_politica(texto_politica: str) -> str:
    primeira_linha = texto_politica.splitlines()[0] if texto_politica else ""
    return primeira_linha.split(":", 1)[1].strip() if ":" in primeira_linha else ""


@dataclass
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


@dataclass
class Dominio:
    salas: list[dict] = field(default_factory=_carregar_salas)
    reservas: list[Reserva] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _proximo_id: int = field(default=1)

    def __post_init__(self) -> None:
        self.reservas = [Reserva(**r) for r in _carregar_reservas_iniciais()]
        self._proximo_id = len(self.reservas) + 1
        self.politica_texto = carregar_politica()
        self.politica_versao = versao_politica(self.politica_texto)

    def sala_por_id(self, sala_id: str) -> dict | None:
        return next((s for s in self.salas if s["id"] == sala_id), None)

    def _proximo_id_reserva(self) -> str:
        rid = f"res-{self._proximo_id:04d}"
        self._proximo_id += 1
        return rid

    def conflitos(self, sala_id: str, inicio: datetime, fim: datetime, ignorar: set[str] | None = None) -> list[Reserva]:
        ignorar = ignorar or set()
        achados = []
        for r in self.reservas:
            if r.sala != sala_id or r.id in ignorar:
                continue
            r_inicio = datetime.fromisoformat(r.inicio)
            r_fim = datetime.fromisoformat(r.fim)
            if inicio < r_fim and r_inicio < fim:
                achados.append(r)
        return achados

    def alternativas(self, sala_id: str, inicio: datetime, fim: datetime) -> list[str]:
        pedida = self.sala_por_id(sala_id)
        capacidade_minima = pedida["capacidade"] if pedida else 0
        candidatas = []
        for sala in self.salas:
            if sala["id"] == sala_id:
                continue
            if sala["capacidade"] < capacidade_minima:
                continue
            if self.conflitos(sala["id"], inicio, fim):
                continue
            candidatas.append(sala)
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]

    def criar_reserva(self, sala_id: str, inicio: datetime, fim: datetime, responsavel: str) -> Reserva:
        with self._lock:
            reserva = Reserva(
                id=self._proximo_id_reserva(),
                sala=sala_id,
                inicio=inicio.isoformat(),
                fim=fim.isoformat(),
                responsavel=responsavel,
            )
            self.reservas.append(reserva)
            return reserva


def _hora_sp(dt: datetime) -> time:
    return dt.astimezone(FUSO_SP).timetz().replace(tzinfo=None)


def validar_pedido(dominio: Dominio, sala_id: str, inicio_str: str, fim_str: str) -> tuple[datetime, datetime]:
    sala = dominio.sala_por_id(sala_id)
    if sala is None:
        raise ErroDeNegocio(ERRO_SALA.format(sala=sala_id))

    try:
        inicio = datetime.fromisoformat(inicio_str)
        fim = datetime.fromisoformat(fim_str)
    except ValueError:
        raise ErroDeNegocio(ERRO_INTERVALO) from None

    if fim <= inicio:
        raise ErroDeNegocio(ERRO_INTERVALO)

    if not (JANELA_INICIO <= _hora_sp(inicio) and _hora_sp(fim) <= JANELA_FIM):
        raise ErroDeNegocio(ERRO_JANELA)

    if fim - inicio > DURACAO_MAXIMA:
        raise ErroDeNegocio(ERRO_DURACAO)

    return inicio, fim
