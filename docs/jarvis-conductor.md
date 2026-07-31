# Jarvis Conductor

Camada aditiva que vigia as sessões ociosas do **GitHub Copilot app** e as mantém
andando: retoma o que é rotina, promove o que travou, e escala para o humano o que
tem risco.

Nada aqui refatora o núcleo do OpenJarvis — são módulos novos mais um punhado de
registros.

## Como usar

```powershell
# um tick, escopo default (a pasta de projetos), o que não é trivial pede aprovação
.\scripts\jarvis-conductor.ps1

# só observar
.\scripts\jarvis-conductor.ps1 -ReportOnly

# o que este atalho vai fazer? (não executa nada, não precisa de token)
.\scripts\jarvis-conductor.ps1 -PrintPlan

# criar o atalho na área de trabalho
.\scripts\jarvis-conductor.ps1 -InstallShortcut
```

## Onde mora a segurança

A invariante "zero tools no resume" foi **abandonada** — era o requisito invertido.
As sessões são do dono, fazendo o trabalho dele, com as permissões que ele concedeu
ao abrir cada uma. Uma sessão desarmada não continua nada, que é o objetivo de
retomá-la. `--resume` restaurar as capacidades da sessão **não é bug**.

A segurança está em **escolher o que responder**, não em desarmar o que já roda:

| Camada | O que faz |
|---|---|
| Elegibilidade | só sessões do app (`host_type='github'`), nunca a própria, dentro de `--allowed-root` |
| Tier | trivial responde sozinho; sensível (prod/infra/secrets) enfileira e **avisa o dono** |
| Claim/lease | um turno é respondido no máximo uma vez, mesmo com N processos |
| Ativação | um tick por clique. Sem escuta ativa, sem daemon |

O sandbox (`sandboxed=True`) sobrevive **só** para uma sessão **nova** que o conductor
abra sozinho — aí nada foi concedido ainda. `--allow-all-tools` nunca entra por padrão.

### O que ainda NÃO existe: contenção

Selecionar não é conter. O turno retomado roda no processo que já tem o token, com as
ferramentas da sessão. Isso é aceitável enquanto **há um humano no clique** — o raio de
alcance é um tick.

Um loop infinito que responde sozinho seria outra coisa, e é o modo cujo único freio
real seria isolamento de SO (usuário sem privilégio, container, credencial escopada).
Como isso não existe, esse modo é **recusado programaticamente** (`check_unattended`).
Continuam abertos: um tick por clique, observação contínua (`--dry-run`), e um loop de
duração declarada (`--max-ticks N`) — declarar torna a escolha visível na linha de
comando em vez de implícita.

## Achados de ambiente (custaram caro, valem para qualquer automação do CLI)

1. **Prompt com quebra de linha faz o `--resume` abrir sessão NOVA** em vez de anexar.
   Isolado com mesma sessão/cwd/env/argv: 1 linha anexa, 5 linhas cria outra. Guard:
   `test_default_prompt_is_a_single_line`.
2. **`--available-tools=` vazio é silenciosamente ignorado** — o CLI aceita e executa
   ferramenta assim mesmo. O que bloqueia é `--excluded-tools=<nomes>` **+**
   `--disable-builtin-mcps`; sem o segundo, o agente dá a volta pelo MCP do GitHub.
3. **Um filho `copilot` herda os plugins/hooks da máquina.** O hook do voice-chat
   bloqueia turno sem a tool `falar`, que não existe em filho headless → `UNATTENDED_ENV`.
4. **O turno é gravado depois que o processo sai** → read-back precisa de polling.
5. **`--resume` custa ~4× tokens** e escala com o histórico → timeout adaptativo.

## Testes

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/conductor tests/agents/test_copilot_cli.py tests/tools/test_copilot_sessions.py `
  tests/speech/test_vox_engine.py tests/memory/test_native_java.py tests/speech/test_discovery.py
```

Os 2 skips são os contract tests, que gastam quota real:

```powershell
$env:RUN_COPILOT_CONTRACT='1'   # exige o CLI no PATH e GH_TOKEN/GITHUB_TOKEN
```

Eles provam o que nenhum fake prova: que o footer do CLI ainda expõe os campos que o
parser usa, e que `--resume` **anexa** na sessão-alvo em vez de abrir outra.

### Regra desta camada: o teste tem que morrer com a mutação

Todo teste de invariante de segurança precisa **falhar quando a invariante é removida**.
Verificado à mão em cada um:

| Mutação | Teste que fica vermelho |
|---|---|
| tirar a checagem de expiração do gate | `test_approval_past_its_deadline_does_not_authorise` |
| não marcar a aprovação como `executed` | `test_approval_authorises_exactly_once` |
| forçar `--dry-run` no launcher | `test_the_shortcut_path_conducts` |
| apertar o escopo default pro checkout | `test_the_shortcut_path_is_scoped` |
| `check_unattended` virar no-op | `test_an_endless_answering_loop_is_refused` (e a suíte **trava**, que é o ponto) |

Um teste verde que sobrevive à mutação não prova nada. Foi assim que uma regressão
passou: um "default seguro" fez o atalho parar de conduzir e virar um relatório, sem
nenhum teste ficar vermelho.

## Módulos

| Arquivo | Papel |
|---|---|
| `conductor/models.py` | tipos puros (`SessionSnapshot`, `TickReport`, `RetryPolicy`) |
| `conductor/ports.py` | as interfaces que o núcleo consome |
| `conductor/policy.py` | elegibilidade e tier |
| `conductor/promotion.py` | detecta sessão travada em perguntas e liga o modo-auto dela |
| `conductor/state.py` | claim store SQLite (`BEGIN IMMEDIATE`, lease, recuperação) |
| `conductor/service.py` | o tick: elegibilidade → claim → tier → aprovação → resume → read-back |
| `conductor/adapters.py` | as bordas reais (CLI, aprovações, memória, voz) |
| `conductor/runner.py` | entry point, singleton, e o que é recusado antes de começar |
| `agents/copilot_cli.py` | dirige o `copilot` como agente |
| `speech/vox_engine.py` | voz pt-BR local pelo SDK do vox-engine |
| `tools/storage/native_java.py` | memória com escopo de projeto |
| `tools/copilot_sessions.py` | leitura read-only do `session-store.db` do app |
