# 📚 Biblioteca 2º Mtec 2026 — Turma B

Sistema de gestão de biblioteca escolar, desenvolvido em **Laravel** pelos
estudantes do 2º ano do curso técnico em Desenvolvimento de Sistemas (Etec),
como projeto integrador entre as disciplinas de **Programação** e
**Banco de Dados**.

## Sobre o projeto

A escola precisa controlar seu acervo de livros, os exemplares físicos
disponíveis, os leitores cadastrados e o ciclo de empréstimo e devolução —
hoje um processo manual e sujeito a erro. Este projeto constrói um sistema
web para resolver isso: cadastro de livros/autores/gêneros, controle de
exemplares por cópia física, cadastro de leitores e funcionários com
diferentes níveis de acesso, o fluxo completo de empréstimo/devolução com
regras de negócio (faixa etária, limite de empréstimos simultâneos, prazos e
atrasos) e relatórios gerenciais para a equipe da biblioteca.

O projeto tem dois objetivos didáticos simultâneos:

- **Metodologia ágil (Scrum)** — o trabalho está organizado em 4 sprints de
  2 semanas, com issues, milestones, papéis rotativos e cerimônias
  (Planning, Daily, Review, Retrospectiva). Veja as
  [milestones](../../milestones) e as [issues](../../issues) do repositório.
- **Laravel e PHP** — a aplicação é construída sobre um modelo de dados já
  definido (ver `docs/` assim que o diagrama for adicionado), integrando com
  o trabalho da disciplina de Banco de Dados.

Este repositório é a Turma B. Existe uma segunda turma trabalhando no
mesmo backlog, em paralelo e de forma independente, com issues e regras
idênticas — cada turma entrega sua própria implementação.

**Prazo final: 15/11/2026** (apresentação do projeto).

## Stack técnica

- **Laravel 12** + PHP 8.3
- **Blade** + **Tailwind CSS** (via Laravel Breeze)
- **MySQL 8**
- **Vite** para build de assets front-end
- **GitHub Actions** para CI (veja "Regras do projeto" abaixo)

## Como contribuir

1. Escolha uma issue aberta e sem responsável, atribua-a a si mesmo.
2. Crie uma branch a partir da `main` (padrão de nome: `tipo/issue-NN-descricao-curta`).
3. Faça commits pequenos e frequentes.
4. Abra um Pull Request para a `main` e peça revisão do time.
5. Corrija o que o time e os checks automáticos (abaixo) apontarem.
6. O PR é mesclado assim que aprovado e com os checks verdes.

Consulte `docs/fluxo-git.md` (a ser escrito pelo time, veja as issues de
Infra da Sprint 1) para o passo a passo completo.

## Regras do projeto (proteção de branch e CI)

A branch `main` é protegida. Nenhum push direto é aceito — toda mudança
entra por Pull Request. Para um PR ser aceito, **todas** as regras abaixo
precisam estar satisfeitas ao mesmo tempo:

### 1. Três aprovações
Um Pull Request só pode ser mesclado depois de **aprovado por pelo menos 3
integrantes do time** (não conta a própria pessoa que abriu o PR).

### 2. Política de tamanho de Pull Request (`verificar-politica-pr`)
Um bot analisa automaticamente o diff do PR e reprova quando:
- **Um arquivo já existente é reescrito quase por inteiro** (90% ou mais das
  suas linhas alteradas). Prefira mudanças pequenas e incrementais no lugar
  de reescrever um arquivo do zero.
- **O PR é grande** (300 linhas alteradas ou mais) **e** inclui arquivos que
  já existiam antes do PR. PRs grandes só são aceitos quando **todos** os
  arquivos tocados são novos (criação) — por exemplo, adicionar várias
  telas novas de uma vez é permitido; reescrever várias telas existentes de
  uma vez, não.
- Arquivos gerados automaticamente (`composer.lock`, `package-lock.json`,
  migrations) não entram nesse cálculo.

O resultado aparece como um comentário automático no próprio PR, explicando
exatamente o que precisa ser dividido ou ajustado.

### 3. Code Intelligence — qualidade de código (`code-intelligence`)
Um segundo bot roda uma análise estática do código PHP alterado no PR,
comparando com a `main`, e **reprova se o PR introduzir qualquer violação
nova** que não existia antes (tolerância zero) — por exemplo:

- Nomes de variável/parâmetro/método com menos de 3 caracteres
- Classe ou método sem comentário de documentação (`/** ... */`)
- Mais de uma classe por arquivo, ou classe com nome diferente do arquivo
- Funções/arquivos muito longos (acima dos limites configurados)

Templates Blade (`*.blade.php`) e migrations (`database/migrations/**`) são
**excluídos** dessa análise — a ferramenta analisa apenas código PHP
"normal" (models, controllers, requests, services, etc). A configuração
completa está em `.code-intelligence.json`, na raiz do repositório.

O resultado também aparece como um comentário automático no PR, listando
cada violação nova com arquivo, linha e explicação. Corrigir o problema e
dar um novo push reavalia automaticamente — não é preciso fechar e reabrir
o PR.

### 4. Issue vinculada (`verificar-issue-vinculada`)
Todo PR **precisa** referenciar, na descrição, a issue que ele resolve,
usando uma das palavras-chave de fechamento automático do GitHub:
`Closes #N`, `Fixes #N` ou `Resolves #N` (aceita variações como
`closed`/`fixed`/`resolved`, em qualquer posição do texto). O número
precisa ser de uma issue existente e aberta no repositório.

Isso não é burocracia: é o que faz o **próprio GitHub fechar a issue
automaticamente** assim que o PR é mesclado na `main` — ninguém precisa
fechar issues manualmente. O template de PR do repositório já vem com o
campo `Closes #` pronto para preencher.

### O que fazer se o PR for reprovado
1. Leia o comentário do bot que reprovou (política de PR, Code
   Intelligence ou Issue Vinculada) — ele explica exatamente o motivo.
2. Corrija o código, a descrição do PR, ou divida o PR em partes menores.
3. Dê um novo `git push` (ou edite a descrição do PR) — os checks rodam de
   novo automaticamente.
4. Se você acha que a reprovação é um falso positivo, converse com o
   professor antes de tentar contornar a regra.

## Modelo de dados

_(o diagrama ER e a documentação do schema serão adicionados aqui pelo time
— veja as issues de Banco de Dados da Sprint 1)._

## Equipe

Cada integrante do time adiciona sua própria linha abaixo, através de um
Pull Request individual (veja a issue "Adicionar seu nome ao README").

| Nome completo | Nick | Registro de matrícula |
|---|---|---|
| _adicione sua linha aqui_ | | |
