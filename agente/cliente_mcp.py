"""Cliente MCP puro: o agente como host, falando Streamable HTTP de verdade
com o servidor MCP por fora do processo. Nada de importar a tool direto.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_TRACEPARENT = "traceparent"


class ErroMcp(Exception):
    def __init__(self, code: int | None, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _url() -> str:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "7301")
    caminho = os.environ.get("MCP_PATH", "/mcp")
    return f"http://{host}:{port}{caminho}"


def _headers(metodo: str, nome: str | None) -> dict:
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO,
        "Mcp-Method": metodo,
    }
    if nome is not None:
        cabecalhos["Mcp-Name"] = nome
    return cabecalhos


def _meta(traceparent: str | None) -> dict:
    meta = {
        META_PROTOCOL_VERSION: PROTOCOLO,
        META_CLIENT_INFO: CLIENT_INFO,
        META_CLIENT_CAPABILITIES: CAPABILITIES,
    }
    if traceparent:
        meta[META_TRACEPARENT] = traceparent
    return meta


def _chamar(metodo: str, params: dict, nome: str | None = None) -> dict:
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": params}
    dados = json.dumps(corpo).encode("utf-8")
    requisicao = urllib.request.Request(_url(), data=dados, headers=_headers(metodo, nome), method="POST")
    try:
        with urllib.request.urlopen(requisicao, timeout=30) as resposta:
            bruto = resposta.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        bruto = exc.read().decode("utf-8")
    resposta_json = json.loads(bruto)
    if "error" in resposta_json:
        erro = resposta_json["error"]
        raise ErroMcp(erro.get("code"), erro.get("message", ""), erro.get("data"))
    return resposta_json["result"]


def tools_list(traceparent: str | None = None) -> dict:
    return _chamar("tools/list", {"_meta": _meta(traceparent)})


def resources_read(uri: str, traceparent: str | None = None) -> dict:
    return _chamar("resources/read", {"uri": uri, "_meta": _meta(traceparent)}, nome=uri)


def tools_call(
    nome: str,
    argumentos: dict,
    traceparent: str | None = None,
    input_responses: dict | None = None,
    request_state: str | None = None,
) -> dict:
    params: dict = {"name": nome, "arguments": argumentos, "_meta": _meta(traceparent)}
    if input_responses is not None:
        params["inputResponses"] = input_responses
    if request_state is not None:
        params["requestState"] = request_state
    return _chamar("tools/call", params, nome=nome)
