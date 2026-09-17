"""Selagem do requestState do MRTR.

O requestState viaja pelas maos do cliente entre o input_required e o retry,
entao e entrada controlada por atacante. Ele carrega tudo que o servidor
precisa para reconstruir o pedido original, e nada fica guardado em memoria
no meio do caminho: um restart do processo nao invalida um retry legitimo.

Integridade por HMAC-SHA256 com a chave de REQUEST_STATE_SECRET (nunca
hardcoded) e expiracao embutida no payload assinado. Qualquer alteracao de
um unico byte no token quebra a assinatura e o retry e rejeitado com
-32602, exatamente como a spec exige.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

PREFIXO = "v1"
TTL_SEGUNDOS = 15 * 60  # entre 5 e 30 minutos, como pede o enunciado


class RequestStateInvalido(Exception):
    """requestState adulterado, malformado ou expirado."""


def _chave_secreta() -> bytes:
    valor = os.environ.get("REQUEST_STATE_SECRET")
    if not valor:
        raise RuntimeError(
            "REQUEST_STATE_SECRET nao esta definida. Gere uma com "
            "python3 -c \"import secrets; print(secrets.token_hex(32))\" "
            "e exporte antes de subir o servidor MCP."
        )
    if len(valor) < 32:
        raise RuntimeError("REQUEST_STATE_SECRET precisa ter no minimo 32 bytes de aleatoriedade.")
    return valor.encode("utf-8")


def _b64u(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


def _b64u_decode(texto: str) -> bytes:
    resto = -len(texto) % 4
    return base64.urlsafe_b64decode(texto + "=" * resto)


def selar(payload: dict) -> str:
    corpo = {**payload, "exp": int(time.time()) + TTL_SEGUNDOS}
    corpo_bytes = json.dumps(corpo, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assinatura = hmac.new(_chave_secreta(), corpo_bytes, hashlib.sha256).digest()
    return f"{PREFIXO}.{_b64u(corpo_bytes)}.{_b64u(assinatura)}"


def abrir(token: str) -> dict:
    try:
        prefixo, corpo_b64, assinatura_b64 = token.split(".", 2)
        if prefixo != PREFIXO:
            raise ValueError("prefixo desconhecido")
        corpo_bytes = _b64u_decode(corpo_b64)
        assinatura = _b64u_decode(assinatura_b64)
    except Exception as exc:
        raise RequestStateInvalido("requestState malformado") from exc

    esperada = hmac.new(_chave_secreta(), corpo_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(assinatura, esperada):
        raise RequestStateInvalido("requestState com assinatura invalida")

    try:
        payload = json.loads(corpo_bytes)
    except json.JSONDecodeError as exc:
        raise RequestStateInvalido("requestState com corpo invalido") from exc

    if payload.get("exp", 0) < time.time():
        raise RequestStateInvalido("requestState expirado")

    return payload
