# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (MBA Engenharia de Software com IA, curso de MCP e A2A). Central de Salas da Hill Valley Tech: um servidor MCP que expõe as salas como capacidade, e um agente que consome esse servidor por dentro (host MCP) e se oferece por fora como servidor A2A (`reservar-sala`).

Stack: Python 3.10+. O servidor MCP usa o SDK oficial `mcp` (v2.2.0, revisão de protocolo `2026-07-28`) mais `uvicorn`, com versões travadas em `servidor-mcp/pyproject.toml`. O agente usa só biblioteca padrão (`http.server`, `urllib`, `json`) nos dois papéis (host MCP e servidor A2A) — o porquê está em "Decisões técnicas". Sem ORM, sem banco, sem dependência de SDK de LLM.

## Como rodar

A partir de um clone limpo, em dois terminais.

**1. Gere a chave de integridade do `requestState`** (32 bytes de aleatoriedade, nunca hardcoded):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**2. Terminal 1 — servidor MCP** (porta `7301`). Ele depende do SDK oficial, então crie um venv e instale antes de subir:

```bash
cd servidor-mcp
python3 -m venv .venv
source .venv/bin/activate        # no Windows: .venv\Scripts\activate
pip install .
export REQUEST_STATE_SECRET="<a chave que voce gerou no passo 1>"
python3 server.py
```

No PowerShell: `$env:REQUEST_STATE_SECRET = "<chave>"` antes do `python server.py`.

**3. Terminal 2 — agente** (porta `7300`; precisa do servidor MCP já respondendo). Não tem dependência externa, roda com o Python do sistema:

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

Do lado do servidor, `servidor-mcp/server.py::escolha_de_sala` é o resolver que decide, por regra de negócio, se `reservar_sala` segue direto ou precisa perguntar uma alternativa: sem conflito, devolve a própria sala pedida; com conflito e alternativas, devolve `Elicit(...)`, e o SDK (`MCPServer`/`Resolve`) é quem transforma isso em `resultType: input_required` com `inputRequests`/`requestState`, porque a versão negociada é `>= 2026-07-28`.

Do lado do agente, a costura mora em `agente/server.py`, nas duas funções que dão nome à seção "5. A ponte" do enunciado:

- **`_pausar()`** é onde o `input_required` do MCP se transforma em `TASK_STATE_INPUT_REQUIRED`. Ela lê a chave e o `enum` do `inputRequests` devolvido pelo servidor MCP, monta a linha `alternativas: <ids>` como mensagem da Task, e guarda o `requestState` opaco dentro de uma `Pendencia` associada àquela Task especificamente (nunca num estado global) — é isso que faz duas Tasks pausadas ao mesmo tempo nunca trocarem de `requestState` entre si (critério de aceite da ponte, verificação 33 do validador).
- **`continuar_escolha()`** é onde a resposta do cliente A2A (`escolha=<valor>`) volta a ser um `tools/call` novo contra o servidor MCP: o `requestState` guardado é ecoado sem modificação, o `inputResponses` leva a mesma chave que veio no `inputRequests`, e o id de JSON-RPC é gerado de novo a cada chamada dentro de `cliente_mcp._chamar()` — nunca reaproveitado do request original.

O `traceparent` entra em `_send_message()` (mesmo arquivo): quando o cliente A2A manda o header, `_propagar_traceparent()` preserva o trace-id e gera um span-id novo, e esse valor fica guardado na própria Task para ser usado em todos os requests MCP daquela Task, incluindo o retry.

## Decisões técnicas

- **Servidor MCP sobre o SDK oficial de verdade.** `servidor-mcp/server.py` usa `mcp.server.mcpserver.MCPServer` (pacote `mcp` v2.2.0, a v2 alinhada à revisão `2026-07-28`). O transporte Streamable HTTP, a validação dos campos obrigatórios de `_meta`, o espelhamento dos headers `Mcp-Method`/`Mcp-Name` (com `-32020` na divergência) e o ciclo inteiro de MRTR (`InputRequiredResult`, a checagem de capability com `-32021` e a mensagem exata "Client did not declare the form elicitation capability required by resolver '...'") são resolvidos pelo próprio framework, não reescritos na mão. A única peça de negócio é o resolver `escolha_de_sala` (`Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)]`), descrito acima em "Onde a ponte acontece". Isso só foi possível depois de instalar o SDK e ler o código-fonte para confirmar, com evidência, que a revisão `2026-07-28` do enunciado é exatamente a que essa versão implementa (não é uma invenção do curso).
- **`requestState`**: selado pelo próprio SDK, via `mcp.server.request_state.RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=900)`. O codec padrão é AES-256-GCM com a chave derivada por HKDF-SHA256 — então o `requestState` é cifrado, não só assinado, e qualquer adulteração quebra a tag de autenticação. O TTL configurado é de 15 minutos, dentro da janela de 5–30 exigida. O framework também amarra o token aos argumentos exatos da chamada (`tools/call`, nome da tool, hash dos `arguments`): se o cliente reenviar `arguments` diferentes no retry, o token é rejeitado por inteiro com `-32602` em vez de silenciosamente ignorar a divergência — um dos dois caminhos que o enunciado aceita para "argumentos adulterados não tomam efeito". A chave vem só de `REQUEST_STATE_SECRET`; o servidor recusa subir sem ela. Testado manualmente: gerar o conflito, matar o processo do servidor MCP, subir de novo com a mesma `REQUEST_STATE_SECRET`, e o retry ainda conclui a reserva.
- **Por que o agente não usa `mcp.client.ClientSession` para o lado host.** Tentei: `ClientSession.list_tools()` não aceita um `meta` por chamada (só `call_tool()` aceita), e `ClientSession.initialize()` faz o handshake clássico de sessão — na minha instalação ele negociou para a revisão `2025-11-25`, não a `2026-07-28` sem sessão que o enunciado pede. A API de alto nível do cliente é desenhada em torno de uma versão negociada uma vez no `initialize` e reaproveitada depois, o que contraria "nenhum dos dois lados pode inferir versão... de conexão aberta". Em vez de forçar esse encaixe, `agente/cliente_mcp.py` fala Streamable HTTP direto com `urllib`, declarando `_meta` e os headers em toda chamada — o mesmo formato de wire que o servidor (agora real) produz e consome, verificado byte a byte contra `exemplos/wire/`. A seção "A2A v1.0" do enunciado não nomeia um SDK obrigatório, então o lado A2A do agente (`SendMessage`/`GetTask`/Task) segue com biblioteca padrão sem essa mesma ressalva.
- **Estado das Tasks**: em memória, dentro de `agente/tarefas.py` (`Tarefas`, um dicionário `id -> Task`). Não sobrevive a um restart do agente, o que é aceitável pelo enunciado (só o `requestState` precisa sobreviver a restart, e esse mora no servidor MCP, opaco para o agente).
- **Dois processos de fato**: o agente nunca importa uma função do servidor MCP; toda chamada passa por `agente/cliente_mcp.py`, que fala HTTP com `urllib`.

## Saída do validador

Execução com os dois processos recém-iniciados:

```
trace-id desta execucao: 632966850842eddd21985c14e7eb4dc1
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
