# Fase 0 — as medições que decidiram a arquitetura

Medido em 31/07/2026, nesta máquina (Ryzen 7 5800XT, 32 GB, RTX 5060, Windows).
Reproduzir com `python scripts/measure_concurrency.py 1 2 3`.

O plano da mesa de ADR proibia construir o cérebro concorrente antes deste
go/no-go. Ele decide uma coisa: **o maestro pode responder sessões em paralelo,
ou tem que ser uma fila?**

## -1a · Pattern 1 (runtime por sessão) ou Pattern 2 (compartilhado)?

**Pattern 2.** Cada sessão adicional custa ~2,6 processos e ~350 MB, não um
runtime inteiro.

| N | processos (base → pico) | RSS (base → pico) |
|---|---|---|
| 1 | 149 → 152 (+3) | 7700 → 8045 MB (+345) |
| 2 | 149 → 153 (+4) | 7654 → 8312 MB (+658) |
| 3 | 149 → 157 (+8) | 7683 → 8743 MB (+1060) |

## -1b · Concorrência acelera?

**Sim, quase linear.**

| N | wall-clock | se fosse serial | razão |
|---|---|---|---|
| 1 | 29,1 s | — | — |
| 2 | 25,0 s | 58,2 s | **43 %** |
| 3 | 24,0 s | 87,3 s | **27 %** |

## Veredito

**Concorrência é viável; o conductor não precisa ser fila serializada.**

As ~7 sessões do dono custariam ~2,4 GB, contra ~9,9 GB livres na medição.
A premissa sob a qual o cérebro foi construído estava certa — mas era premissa,
e agora é medida.

## O que este número NÃO decide

- **Custo em tokens.** Wall-clock e RAM não dizem nada sobre quota. Um ciclo de
  trabalho real precisa ser medido em tokens antes de ligar qualquer modo
  contínuo; o teto da assinatura é outro limite, e mais provável de morder.
- **Concorrência com sessões GRANDES.** O prompt aqui é trivial. Um `--resume`
  custa ~4× mais tokens e escala com o histórico — o perfil de memória de três
  sessões longas simultâneas não foi medido.
