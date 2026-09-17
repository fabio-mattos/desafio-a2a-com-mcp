"""Task A2A: identidade, estado e produto. O requestState guardado aqui e
opaco para o agente -- ele so guarda e ecoa, nunca abre.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

ESTADOS_TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pendencia:
    """O que fica pausado numa Task em TASK_STATE_INPUT_REQUIRED."""

    chave: str
    request_state: str
    alternativas: list[str]
    traceparent: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


@dataclass
class Task:
    id: str
    context_id: str
    traceparent: str
    status_state: str = "TASK_STATE_SUBMITTED"
    status_message: dict | None = None
    history: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    pendencia: Pendencia | None = None

    def to_dict(self) -> dict:
        status: dict = {"state": self.status_state}
        if self.status_message is not None:
            status["message"] = self.status_message
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }


class Tarefas:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def criar(self, traceparent: str) -> Task:
        task = Task(id=novo_id("task"), context_id=novo_id("ctx"), traceparent=traceparent)
        self._tasks[task.id] = task
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)
