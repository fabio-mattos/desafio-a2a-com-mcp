# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (MBA Engenharia de Software com IA, curso de MCP e A2A). Central de Salas da Hill Valley Tech: um servidor MCP que expõe as salas como capacidade, e um agente que consome esse servidor por dentro (host MCP) e se oferece por fora como servidor A2A (`reservar-sala`).

Stack: Python 3.10+, apenas biblioteca padrão nos dois processos (`http.server`, `urllib`, `hmac`, `json`). Sem ORM, sem banco, sem dependência de SDK de LLM.

## Como rodar

A partir de um clone limpo, em dois terminais.

**1. Gere a chave de integridade do `requestState`** (32 bytes de aleatoriedade, nunca hardcoded):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**2. Terminal 1 — servidor MCP** (porta `7301`):

```bash
export REQUEST_STATE_SECRET="<a chave que voce gerou no passo 1>"
python3 servidor-mcp/server.py
```

No PowerShell: `$env:REQUEST_STATE_SECRET = "<chave>"` antes do `python servidor-mcp/server.py`.

**3. Terminal 2 — agente** (porta `7300`; precisa do servidor MCP já respondendo):

```bash
python3 agente/server.py
```

**4. Validador**, a partir da raiz do repositório:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Suba os dois processos do zero antes de validar: reservas criadas numa execução mudam o resultado da seguinte.

Variáveis de ambiente opcionais (os padrões já são os exigidos pelo enunciado):

| Variável | Padrão | Processo |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `127.0.0.1` / `7301` / `/mcp` | servidor MCP e agente (para achar o servidor) |
| `AGENT_HOST` / `AGENT_PORT` / `A2A_PATH` | `127.0.0.1` / `7300` / `/a2a` | agente |
| `REQUEST_STATE_SECRET` | *(obrigatória, sem padrão)* | servidor MCP |

## Onde a ponte acontece

Toda a costura mora em `agente/server.py`, nas duas funções que dão nome à seção "5. A ponte" do enunciado:

- **`_pausar()`** é onde o `input_required` do MCP se transforma em `TASK_STATE_INPUT_REQUIRED`. Ela lê a chave e o `enum` do `inputRequests` devolvido pelo servidor MCP, monta a linha `alternativas: <ids>` como mensagem da Task, e guarda o `requestState` opaco dentro de uma `Pendencia` associada àquela Task especificamente (nunca num estado global) — é isso que faz duas Tasks pausadas ao mesmo tempo nunca trocarem de `requestState` entre si (critério de aceite da ponte, verificação 33 do validador).
- **`continuar_escolha()`** é onde a resposta do cliente A2A (`escolha=<valor>`) volta a ser um `tools/call` novo contra o servidor MCP: o `requestState` guardado é ecoado sem modificação, o `inputResponses` leva a mesma chave que veio no `inputRequests`, e o id de JSON-RPC é gerado de novo a cada chamada dentro de `cliente_mcp._chamar()` — nunca reaproveitado do request original.

O `traceparent` entra em `_send_message()` (mesmo arquivo): quando o cliente A2A manda o header, `_propagar_traceparent()` preserva o trace-id e gera um span-id novo, e esse valor fica guardado na própria Task para ser usado em todos os requests MCP daquela Task, incluindo o retry.

## Decisões técnicas

- **Sem SDK oficial de MCP/A2A instalado.** As versões descritas no enunciado (revisão MCP `2026-07-28`, MRTR/`input_required`, os códigos `-32020`/`-32021`, o espelhamento de `Mcp-Method`/`Mcp-Name`) são um recorte específico do curso que não corresponde a um pacote publicado que eu conseguisse instalar. Para não arriscar reescrever o protocolo por baixo de uma abstração que não fala exatamente essa revisão, implementei o transporte Streamable HTTP e o binding JSON-RPC do A2A diretamente contra os contratos de `exemplos/wire/` e `validador/validar.py`, usando só a biblioteca padrão do Python nos dois processos. Cada regra do enunciado (MRTR, elicitation em form mode, capability negotiation por request, máquina de estados da Task) tem uma linha de código correspondente, não uma simulação por fora do SDK.
- **`requestState`**: JSON assinado com HMAC-SHA256 (`servidor-mcp/estado_requisicao.py`). O payload leva o pedido original inteiro (sala, início, fim, responsável, a chave da elicitation e as alternativas seladas) mais um `exp` de 15 minutos, dentro da janela de 5–30 minutos exigida. A chave vem só de `REQUEST_STATE_SECRET`; o servidor recusa subir sem ela. Verificação é `hmac.compare_digest` sobre os bytes exatos do payload — qualquer byte trocado no token invalida a assinatura e o retry cai em `-32602`. Como a integridade e a expiração vivem inteiramente dentro do token, e a chave vem de variável de ambiente, um restart do processo entre o `input_required` e o retry não invalida um token legítimo (testado manualmente: gerar o conflito, matar o processo, subir de novo com a mesma `REQUEST_STATE_SECRET`, e o retry ainda conclui a reserva).
- **Os argumentos do retry não são confiáveis**: no caminho de retry (`servidor-mcp/server.py::_reservar_retry`), o servidor ignora por completo o campo `arguments` que o cliente reenvia e reconstrói o pedido só a partir do que foi selado no `requestState`.
- **Estado das Tasks**: em memória, dentro de `agente/tarefas.py` (`Tarefas`, um dicionário `id -> Task`). Não sobrevive a um restart do agente, o que é aceitável pelo enunciado (só o `requestState` precisa sobreviver a restart, e esse mora no servidor MCP, opaco para o agente).
- **Dois processos de fato**: o agente nunca importa uma função do servidor MCP; toda chamada passa por `agente/cliente_mcp.py`, que fala HTTP com `urllib`.

## Saída do validador

Execução com os dois processos recém-iniciados:

```
trace-id desta execucao: b2ac87845c389b288ab63d899f9cde3b
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Verificado adicionalmente à mão (fora do validador, conforme o "Fluxo do avaliador" do enunciado): o `traceparent` acima aparece no stderr do servidor MCP nas chamadas feitas pelo agente durante essa mesma execução; um `requestState` capturado de um conflito real continua sendo aceito num retry depois de reiniciar o processo do servidor MCP com a mesma `REQUEST_STATE_SECRET`; e um header `Mcp-Name` que não bate com o corpo é recusado com `-32020`.
