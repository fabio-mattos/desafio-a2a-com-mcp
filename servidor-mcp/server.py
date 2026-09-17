#!/usr/bin/env python3
"""Servidor MCP (Streamable HTTP) da Central de Salas.

Endpoint unico em POST /mcp. Sem sessao: cada request carrega, no `_meta`,
a versao de protocolo e as capabilities do cliente, e o servidor nunca
infere nada de uma chamada anterior.

Suba com:

    python3 servidor-mcp/server.py

Variaveis de ambiente:

    MCP_HOST               padrao 127.0.0.1
    MCP_PORT               padrao 7301
    MCP_PATH               padrao /mcp
    REQUEST_STATE_SECRET   obrigatoria, >= 32 bytes de aleatoriedade
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dominio
import estado_requisicao

PROTOCOLO = "2026-07-28"
CHAVE_ELICITATION = "escolha_de_sala"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_TRACEPARENT = "traceparent"

SERVER_INFO = {"name": "central-de-salas", "version": "1.0.0"}

DOMINIO = dominio.Dominio()

TOOLS = [
    {
        "name": "listar_salas",
        "description": "Lista todas as salas com capacidade e recursos.",
        "inputSchema": {"type": "object", "properties": {}, "title": "listar_salasArguments"},
        "outputSchema": {
            "type": "object",
            "properties": {
                "salas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "nome": {"type": "string"},
                            "capacidade": {"type": "integer"},
                            "recursos": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "nome", "capacidade", "recursos"],
                    },
                }
            },
            "required": ["salas"],
        },
    },
    {
        "name": "consultar_disponibilidade",
        "description": "Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"type": "string"},
                "inicio": {"type": "string"},
                "fim": {"type": "string"},
            },
            "required": ["sala", "inicio", "fim"],
            "title": "consultar_disponibilidadeArguments",
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "sala": {"type": "string"},
                "livre": {"type": "boolean"},
                "conflitos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "inicio": {"type": "string"},
                            "fim": {"type": "string"},
                            "responsavel": {"type": "string"},
                        },
                        "required": ["id", "inicio", "fim", "responsavel"],
                    },
                },
            },
            "required": ["sala", "livre", "conflitos"],
        },
    },
    {
        "name": "reservar_sala",
        "description": "Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"type": "string"},
                "inicio": {"type": "string"},
                "fim": {"type": "string"},
                "responsavel": {"type": "string"},
            },
            "required": ["sala", "inicio", "fim", "responsavel"],
            "title": "reservar_salaArguments",
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "reserva": {"type": ["string", "null"]},
                "reservado": {"type": "boolean"},
                "sala": {"type": ["string", "null"]},
                "inicio": {"type": ["string", "null"]},
                "fim": {"type": ["string", "null"]},
                "responsavel": {"type": ["string", "null"]},
                "politica": {"type": ["string", "null"]},
                "motivo": {"type": ["string", "null"]},
            },
        },
    },
]


class ErroProtocolo(Exception):
    def __init__(self, codigo: int, mensagem: str, http_status: int = 200, dados: dict | None = None) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem
        self.http_status = http_status
        self.dados = dados


def _log(metodo: str, id_: object, traceparent: str | None) -> None:
    print(f"[mcp] method={metodo} id={id_} traceparent={traceparent}", file=sys.stderr, flush=True)


def _extrair_meta(params: dict) -> dict:
    meta = params.get("_meta") or {}
    if META_PROTOCOL_VERSION not in meta or META_CLIENT_CAPABILITIES not in meta:
        raise ErroProtocolo(-32602, "faltam campos obrigatorios em _meta (protocolVersion/clientCapabilities)", 400)
    return meta


def _tem_capability_elicitation_form(meta: dict) -> bool:
    capabilities = meta.get(META_CLIENT_CAPABILITIES) or {}
    elicitation = capabilities.get("elicitation")
    return isinstance(elicitation, dict) and "form" in elicitation


def _resultado_erro_execucao(mensagem: str) -> dict:
    return {
        "content": [{"type": "text", "text": mensagem}],
        "isError": True,
        "resultType": "complete",
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _resultado_reserva(payload: dict) -> dict:
    texto = json.dumps(payload, indent=2, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": payload,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _payload_reserva(reserva: dominio.Reserva | None, reservado: bool, motivo: str | None = None) -> dict:
    if reserva is None:
        return {
            "reserva": None,
            "reservado": reservado,
            "sala": None,
            "inicio": None,
            "fim": None,
            "responsavel": None,
            "politica": None,
            "motivo": motivo,
        }
    return {
        "reserva": reserva.id,
        "reservado": reservado,
        "sala": reserva.sala,
        "inicio": reserva.inicio,
        "fim": reserva.fim,
        "responsavel": reserva.responsavel,
        "politica": DOMINIO.politica_versao,
        "motivo": motivo,
    }


def _ferramenta_listar_salas() -> dict:
    payload = {"salas": DOMINIO.salas}
    texto = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": payload,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _ferramenta_consultar_disponibilidade(argumentos: dict) -> dict:
    try:
        inicio, fim = dominio.validar_pedido(DOMINIO, argumentos.get("sala", ""), argumentos.get("inicio", ""), argumentos.get("fim", ""))
    except dominio.ErroDeNegocio as exc:
        return _resultado_erro_execucao(str(exc))

    conflitos = DOMINIO.conflitos(argumentos["sala"], inicio, fim)
    payload = {
        "sala": argumentos["sala"],
        "livre": not conflitos,
        "conflitos": [{"id": c.id, "inicio": c.inicio, "fim": c.fim, "responsavel": c.responsavel} for c in conflitos],
    }
    texto = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": payload,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _ferramenta_reservar_sala(params: dict, meta: dict) -> dict:
    argumentos = params.get("arguments", {})
    request_state = params.get("requestState")

    if request_state:
        return _reservar_retry(request_state, params.get("inputResponses") or {})
    return _reservar_fresco(argumentos, meta)


def _reservar_fresco(argumentos: dict, meta: dict) -> dict:
    try:
        inicio, fim = dominio.validar_pedido(DOMINIO, argumentos.get("sala", ""), argumentos.get("inicio", ""), argumentos.get("fim", ""))
    except dominio.ErroDeNegocio as exc:
        return _resultado_erro_execucao(str(exc))

    sala_id = argumentos["sala"]
    responsavel = argumentos["responsavel"]
    conflitos = DOMINIO.conflitos(sala_id, inicio, fim)
    if not conflitos:
        reserva = DOMINIO.criar_reserva(sala_id, inicio, fim, responsavel)
        return _resultado_reserva(_payload_reserva(reserva, True))

    alternativas = DOMINIO.alternativas(sala_id, inicio, fim)
    if not alternativas:
        return _resultado_erro_execucao(dominio.ERRO_SEM_ALTERNATIVA)

    if not _tem_capability_elicitation_form(meta):
        raise ErroProtocolo(
            -32021,
            f"Client did not declare the form elicitation capability required by resolver '__main__:{CHAVE_ELICITATION}'",
            400,
            dados={"requiredCapabilities": {"elicitation": {"form": {}}}},
        )

    chave = f"__main__:{CHAVE_ELICITATION}"
    token = estado_requisicao.selar(
        {
            "tool": "reservar_sala",
            "sala": sala_id,
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "responsavel": responsavel,
            "chave": chave,
            "alternativas": alternativas,
        }
    )
    propriedade_sala: dict = {"type": "string", "description": "Sala alternativa escolhida"}
    if len(alternativas) == 1:
        propriedade_sala["const"] = alternativas[0]
    else:
        propriedade_sala["enum"] = alternativas

    return {
        "resultType": "input_required",
        "inputRequests": {
            chave: {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"sala": propriedade_sala},
                        "required": ["sala"],
                    },
                },
            }
        },
        "requestState": token,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _reservar_retry(request_state: str, input_responses: dict) -> dict:
    try:
        payload = estado_requisicao.abrir(request_state)
    except estado_requisicao.RequestStateInvalido as exc:
        raise ErroProtocolo(-32602, str(exc)) from exc

    chave = payload["chave"]
    resposta = input_responses.get(chave)
    if resposta is None:
        raise ErroProtocolo(-32602, "inputResponses nao contem a chave esperada pelo requestState")

    action = resposta.get("action")
    sala_id = payload["sala"]
    inicio = datetime.fromisoformat(payload["inicio"])
    fim = datetime.fromisoformat(payload["fim"])
    responsavel = payload["responsavel"]

    if action in ("decline", "cancel"):
        return _resultado_reserva(_payload_reserva(None, False, motivo="recusado"))

    if action != "accept":
        raise ErroProtocolo(-32602, f"action invalida em inputResponses: {action!r}")

    escolhida = (resposta.get("content") or {}).get("sala")
    alternativas_seladas = payload.get("alternativas", [])
    sala_final = escolhida if escolhida in alternativas_seladas else sala_id

    if DOMINIO.conflitos(sala_final, inicio, fim):
        return _resultado_erro_execucao(dominio.ERRO_SEM_ALTERNATIVA)

    reserva = DOMINIO.criar_reserva(sala_final, inicio, fim, responsavel)
    return _resultado_reserva(_payload_reserva(reserva, True))


def _resources_read(params: dict) -> dict:
    uri = params.get("uri")
    if uri != "politica://uso":
        raise ErroProtocolo(-32602, f"resource inexistente: {uri}")
    return {
        "cacheScope": "private",
        "contents": [{"uri": uri, "mimeType": "text/markdown", "text": DOMINIO.politica_texto}],
        "resultType": "complete",
        "ttlMs": 0,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _tools_list() -> dict:
    return {
        "cacheScope": "private",
        "resultType": "complete",
        "tools": TOOLS,
        "ttlMs": 0,
        "_meta": {META_SERVER_INFO: SERVER_INFO},
    }


def _tools_call(params: dict, meta: dict) -> dict:
    nome = params.get("name")
    if nome == "listar_salas":
        return _ferramenta_listar_salas()
    if nome == "consultar_disponibilidade":
        return _ferramenta_consultar_disponibilidade(params.get("arguments", {}))
    if nome == "reservar_sala":
        return _ferramenta_reservar_sala(params, meta)
    raise ErroProtocolo(-32602, f"tool desconhecida: {nome}")


def _validar_headers(headers, metodo: str, params: dict) -> None:
    header_metodo = headers.get("Mcp-Method")
    if header_metodo != metodo:
        raise ErroProtocolo(-32020, f"header Mcp-Method ({header_metodo!r}) nao bate com o metodo do corpo ({metodo!r})", 400)

    if metodo == "tools/call":
        esperado = params.get("name")
        if headers.get("Mcp-Name") != esperado:
            raise ErroProtocolo(-32020, "header Mcp-Name nao bate com o nome da tool no corpo", 400)
    elif metodo == "resources/read":
        esperado = params.get("uri")
        if headers.get("Mcp-Name") != esperado:
            raise ErroProtocolo(-32020, "header Mcp-Name nao bate com a uri no corpo", 400)


def processar(corpo: dict, headers) -> tuple[int, dict]:
    id_ = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}

    try:
        _validar_headers(headers, metodo, params)
        meta = _extrair_meta(params)
        _log(metodo, id_, meta.get(META_TRACEPARENT))

        if metodo == "tools/list":
            resultado = _tools_list()
        elif metodo == "tools/call":
            resultado = _tools_call(params, meta)
        elif metodo == "resources/read":
            resultado = _resources_read(params)
        else:
            raise ErroProtocolo(-32601, f"metodo desconhecido: {metodo}")

        return 200, {"jsonrpc": "2.0", "id": id_, "result": resultado}
    except ErroProtocolo as exc:
        corpo_erro: dict = {"code": exc.codigo, "message": exc.mensagem}
        if exc.dados is not None:
            corpo_erro["data"] = exc.dados
        status = exc.http_status if exc.http_status != 200 else (400 if exc.codigo in (-32602, -32021, -32020) else 200)
        return status, {"jsonrpc": "2.0", "id": id_, "error": corpo_erro}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        caminho_mcp = os.environ.get("MCP_PATH", "/mcp")
        if self.path != caminho_mcp:
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

        status, resposta = processar(corpo, self.headers)
        self._responder(status, resposta)

    def _responder(self, status: int, resposta: dict) -> None:
        dados = json.dumps(resposta).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def main() -> None:
    estado_requisicao._chave_secreta()  # falha rapido se REQUEST_STATE_SECRET nao estiver configurada
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    caminho = os.environ.get("MCP_PATH", "/mcp")
    servidor = ThreadingHTTPServer((host, port), Handler)
    print(f"[mcp] servidor MCP em http://{host}:{port}{caminho}", file=sys.stderr, flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
