#!/usr/bin/env python3
"""Agente Central de Salas.

Por dentro, host MCP: descobre as tools do servidor por tools/list, le o
resource da politica e chama reservar_sala por HTTP, como um cliente MCP de
verdade. Por fora, servidor A2A: publica o Agent Card, aceita SendMessage e
GetTask, e conduz a Task por sua maquina de estados.

A ponte fica em duas funcoes: _pausar() e onde um input_required do MCP
vira TASK_STATE_INPUT_REQUIRED na Task, e continuar_escolha() e onde a
resposta do cliente A2A volta a ser um tools/call novo, com o requestState
ecoado sem modificacao e um id de JSON-RPC diferente do original (o id novo
e gerado dentro de cliente_mcp._chamar a cada chamada).

Suba com:

    python3 agente/server.py

Variaveis de ambiente:

    AGENT_HOST   padrao 127.0.0.1
    AGENT_PORT   padrao 7300
    A2A_PATH     padrao /a2a
    MCP_HOST, MCP_PORT, MCP_PATH  para achar o servidor MCP (padrao 127.0.0.1:7301/mcp)
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cliente_mcp
import pedido
import tarefas

AGENT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "7300"))
A2A_PATH = os.environ.get("A2A_PATH", "/a2a")
AGENT_URL = f"http://{AGENT_HOST}:{AGENT_PORT}{A2A_PATH}"

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": AGENT_URL, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

TAREFAS = tarefas.Tarefas()
LOCK = threading.Lock()
POLITICA_VERSAO = ""


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


# --------------------------------------------------------------------------
# tracing: propaga o trace-id do cliente A2A para dentro do _meta do MCP
# --------------------------------------------------------------------------

def _novo_traceparent(trace_id: str | None = None) -> str:
    trace_id = trace_id or secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-01"


def _propagar_traceparent(recebido: str | None) -> str:
    if recebido:
        partes = recebido.split("-")
        if len(partes) >= 4 and len(partes[1]) == 32:
            return _novo_traceparent(partes[1])
    return _novo_traceparent()


# --------------------------------------------------------------------------
# mensagens A2A
# --------------------------------------------------------------------------

def _msg_agente(texto: str, task: tarefas.Task) -> dict:
    return {
        "messageId": tarefas.novo_id("msg"),
        "role": "ROLE_AGENT",
        "parts": [{"text": texto}],
        "taskId": task.id,
        "contextId": task.context_id,
    }


def _texto_conteudo(resultado: dict) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))


# --------------------------------------------------------------------------
# a ponte: MRTR do MCP <-> TASK_STATE_INPUT_REQUIRED do A2A
# --------------------------------------------------------------------------

def _pausar(task: tarefas.Task, resultado: dict, campos: dict) -> None:
    input_requests = resultado["inputRequests"]
    chave = next(iter(input_requests))
    esquema_sala = input_requests[chave]["params"]["requestedSchema"]["properties"]["sala"]
    alternativas = esquema_sala.get("enum") or ([esquema_sala["const"]] if "const" in esquema_sala else [])

    task.pendencia = tarefas.Pendencia(
        chave=chave,
        request_state=resultado["requestState"],
        alternativas=alternativas,
        traceparent=task.traceparent,
        sala=campos["sala"],
        inicio=campos["inicio"],
        fim=campos["fim"],
        responsavel=campos["responsavel"],
    )
    texto = "alternativas: " + ", ".join(alternativas)
    task.status_state = "TASK_STATE_INPUT_REQUIRED"
    task.status_message = _msg_agente(texto, task)
    task.history.append(task.status_message)


def _finalizar_sucesso(task: tarefas.Task, resultado: dict) -> None:
    sc = resultado["structuredContent"]
    artifact_payload = {
        "reserva": sc["reserva"],
        "sala": sc["sala"],
        "inicio": sc["inicio"],
        "fim": sc["fim"],
        "responsavel": sc["responsavel"],
        "politica": sc["politica"],
    }
    task.artifacts = [
        {
            "artifactId": tarefas.novo_id("art"),
            "name": "reserva",
            "parts": [{"text": json.dumps(artifact_payload, ensure_ascii=False)}],
        }
    ]
    texto = f"Reserva {sc['reserva']} confirmada na {sc['sala']}."
    task.status_state = "TASK_STATE_COMPLETED"
    task.status_message = _msg_agente(texto, task)
    task.history.append(task.status_message)
    task.pendencia = None


def _finalizar_falha(task: tarefas.Task, mensagem: str) -> None:
    task.status_state = "TASK_STATE_FAILED"
    task.status_message = _msg_agente(mensagem, task)
    task.history.append(task.status_message)
    task.pendencia = None


def _finalizar_cancelada(task: tarefas.Task, mensagem: str) -> None:
    task.status_state = "TASK_STATE_CANCELED"
    task.status_message = _msg_agente(mensagem, task)
    task.history.append(task.status_message)
    task.pendencia = None


def iniciar_reserva(task: tarefas.Task, campos: dict) -> None:
    task.status_state = "TASK_STATE_WORKING"
    argumentos = {"sala": campos["sala"], "inicio": campos["inicio"], "fim": campos["fim"], "responsavel": campos["responsavel"]}
    try:
        resultado = cliente_mcp.tools_call("reservar_sala", argumentos, traceparent=task.traceparent)
    except cliente_mcp.ErroMcp as exc:
        _finalizar_falha(task, str(exc))
        return

    if resultado.get("resultType") == "input_required":
        _pausar(task, resultado, campos)
    elif resultado.get("isError"):
        _finalizar_falha(task, _texto_conteudo(resultado))
    else:
        _finalizar_sucesso(task, resultado)


def continuar_escolha(task: tarefas.Task, valor: str) -> None:
    pendencia = task.pendencia
    if pendencia is None:
        raise ErroA2A(-32602, "esta Task nao esta esperando uma escolha")

    argumentos = {"sala": pendencia.sala, "inicio": pendencia.inicio, "fim": pendencia.fim, "responsavel": pendencia.responsavel}

    if valor == "recusar":
        task.status_state = "TASK_STATE_WORKING"
        input_responses = {pendencia.chave: {"action": "decline"}}
        try:
            resultado = cliente_mcp.tools_call(
                "reservar_sala", argumentos, traceparent=pendencia.traceparent,
                input_responses=input_responses, request_state=pendencia.request_state,
            )
        except cliente_mcp.ErroMcp as exc:
            _finalizar_falha(task, str(exc))
            return
        if resultado.get("isError"):
            _finalizar_falha(task, _texto_conteudo(resultado))
        else:
            _finalizar_cancelada(task, "Reserva cancelada a pedido do usuario.")
        return

    if valor not in pendencia.alternativas:
        texto = "alternativas: " + ", ".join(pendencia.alternativas)
        task.status_state = "TASK_STATE_INPUT_REQUIRED"
        task.status_message = _msg_agente(texto, task)
        task.history.append(task.status_message)
        return

    task.status_state = "TASK_STATE_WORKING"
    input_responses = {pendencia.chave: {"action": "accept", "content": {"sala": valor}}}
    try:
        resultado = cliente_mcp.tools_call(
            "reservar_sala", argumentos, traceparent=pendencia.traceparent,
            input_responses=input_responses, request_state=pendencia.request_state,
        )
    except cliente_mcp.ErroMcp as exc:
        _finalizar_falha(task, str(exc))
        return

    if resultado.get("resultType") == "input_required":
        _pausar(task, resultado, argumentos)
    elif resultado.get("isError"):
        _finalizar_falha(task, _texto_conteudo(resultado))
    else:
        _finalizar_sucesso(task, resultado)


# --------------------------------------------------------------------------
# JSON-RPC A2A: SendMessage e GetTask
# --------------------------------------------------------------------------

def _send_message(params: dict, traceparent_recebido: str | None) -> dict:
    message = params.get("message") or {}
    texto = " ".join(p.get("text", "") for p in message.get("parts", []))
    task_id = message.get("taskId")

    with LOCK:
        if task_id:
            task = TAREFAS.obter(task_id)
            if task is None:
                raise ErroA2A(-32602, f"Task inexistente: {task_id}")
            if task.status_state in tarefas.ESTADOS_TERMINAIS:
                raise ErroA2A(-32602, f"Task {task_id} ja esta em estado terminal ({task.status_state})")

            task.history.append(message)
            valor = pedido.parse_escolha(texto)
            if valor is None:
                raise ErroA2A(-32602, "mensagem de continuacao precisa ser 'escolha=<valor>'")
            continuar_escolha(task, valor)
            return {"task": task.to_dict()}

        campos = pedido.parse_reservar(texto)
        if campos is None:
            raise ErroA2A(-32602, "pedido precisa seguir 'reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>'")

        traceparent = _propagar_traceparent(traceparent_recebido)
        task = TAREFAS.criar(traceparent)
        task.history.append(message)
        iniciar_reserva(task, campos)
        return {"task": task.to_dict()}


def _get_task(params: dict) -> dict:
    task_id = params.get("id")
    with LOCK:
        task = TAREFAS.obter(task_id)
        if task is None:
            raise ErroA2A(-32602, f"Task inexistente: {task_id}")
        return {"task": task.to_dict()}


def processar_a2a(corpo: dict, traceparent_recebido: str | None) -> tuple[int, dict]:
    id_ = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    try:
        if metodo == "SendMessage":
            resultado = _send_message(params, traceparent_recebido)
        elif metodo == "GetTask":
            resultado = _get_task(params)
        else:
            raise ErroA2A(-32601, f"metodo desconhecido: {metodo}")
        return 200, {"jsonrpc": "2.0", "id": id_, "result": resultado}
    except ErroA2A as exc:
        return 200, {"jsonrpc": "2.0", "id": id_, "error": {"code": exc.codigo, "message": exc.mensagem}}


# --------------------------------------------------------------------------
# transporte HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/.well-known/agent-card.json":
            self._responder(200, AGENT_CARD)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != A2A_PATH:
            self.send_response(404)
            self.end_headers()
            return

        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            corpo = json.loads(bruto)
        except json.JSONDecodeError:
            self._responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON invalido"}})
            return

        traceparent_recebido = self.headers.get("traceparent")
        status, resposta = processar_a2a(corpo, traceparent_recebido)
        self._responder(status, resposta)

    def _responder(self, status: int, resposta: dict) -> None:
        dados = json.dumps(resposta).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def _descobrir_servidor_mcp() -> None:
    """tools/list + resources/read antes de qualquer tools/call, com retry
    curto para o caso de o servidor MCP ainda estar subindo."""
    global POLITICA_VERSAO
    ultimo_erro: Exception | None = None
    for _ in range(20):
        try:
            ferramentas = cliente_mcp.tools_list()
            nomes = {t["name"] for t in ferramentas.get("tools", [])}
            if "reservar_sala" not in nomes:
                raise RuntimeError(f"servidor MCP nao expoe reservar_sala (tools={sorted(nomes)})")

            recurso = cliente_mcp.resources_read("politica://uso")
            texto_politica = recurso["contents"][0]["text"]
            primeira_linha = texto_politica.splitlines()[0] if texto_politica else ""
            POLITICA_VERSAO = primeira_linha.split(":", 1)[1].strip() if ":" in primeira_linha else ""

            print(f"[agente] tools descobertas via tools/list: {sorted(nomes)}", file=sys.stderr, flush=True)
            print(f"[agente] politica://uso versao {POLITICA_VERSAO}", file=sys.stderr, flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            ultimo_erro = exc
            time.sleep(0.5)
    raise RuntimeError(f"nao consegui falar com o servidor MCP na subida: {ultimo_erro}")


def main() -> None:
    _descobrir_servidor_mcp()
    servidor = ThreadingHTTPServer((AGENT_HOST, AGENT_PORT), Handler)
    print(f"[agente] agente A2A em http://{AGENT_HOST}:{AGENT_PORT}{A2A_PATH}", file=sys.stderr, flush=True)
    print(f"[agente] agent card em http://{AGENT_HOST}:{AGENT_PORT}/.well-known/agent-card.json", file=sys.stderr, flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
