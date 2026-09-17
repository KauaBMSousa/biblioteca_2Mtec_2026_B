#!/usr/bin/env python3
"""Exige que a descrição do PR referencie uma issue com uma palavra-chave de
fechamento automático do GitHub (Closes/Fixes/Resolves #N) — assim, quando o
PR é mesclado, o próprio GitHub fecha a issue automaticamente. Esta issue
precisa existir no repositório e não pode ser, ela mesma, um Pull Request.
"""
import json
import os
import re
import subprocess
import sys

MARCADOR = "<!-- issue-link-bot -->"
PALAVRAS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
PADRAO = re.compile(rf"\b{PALAVRAS}\s*:?\s*#(\d+)", re.IGNORECASE)


def extrair_referencias(corpo: str) -> list[int]:
    return sorted({int(n) for n in PADRAO.findall(corpo or "")})


def issue_existe(repo: str, numero: int) -> tuple[bool, bool]:
    """Retorna (existe_como_issue, esta_aberta)."""
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{numero}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, False
    dado = json.loads(r.stdout)
    if "pull_request" in dado:
        return False, False
    return True, dado.get("state") == "open"


def comentar(repo: str, pr_number: str, corpo: str) -> None:
    lista = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        capture_output=True, text=True,
    )
    comentarios = json.loads(lista.stdout or "[]") if lista.returncode == 0 else []
    existente = next((c for c in comentarios if MARCADOR in c.get("body", "")), None)
    payload = json.dumps({"body": corpo})
    if existente:
        subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/comments/{existente['id']}", "-X", "PATCH", "--input", "-"],
            input=payload, text=True,
        )
    else:
        subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "-X", "POST", "--input", "-"],
            input=payload, text=True,
        )


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    corpo_pr = os.environ.get("PR_BODY", "")

    referencias = extrair_referencias(corpo_pr)

    validas, invalidas, fechadas = [], [], []
    for numero in referencias:
        existe, aberta = issue_existe(repo, numero)
        if not existe:
            invalidas.append(numero)
        elif not aberta:
            fechadas.append(numero)
        else:
            validas.append(numero)

    if validas:
        corpo = (
            f"{MARCADOR}\n"
            "## ✅ Issue vinculada corretamente\n\n"
            f"Este PR fechará automaticamente: {', '.join(f'#{n}' for n in validas)} "
            "quando for mesclado."
        )
        if invalidas or fechadas:
            extras = []
            if invalidas:
                extras.append(f"não encontradas: {', '.join(f'#{n}' for n in invalidas)}")
            if fechadas:
                extras.append(f"já fechadas: {', '.join(f'#{n}' for n in fechadas)}")
            corpo += "\n\n⚠️ Outras referências no texto foram ignoradas (" + "; ".join(extras) + ")."
        comentar(repo, pr_number, corpo)
        sys.exit(0)

    linhas_erro = [
        "Nenhuma referência válida a uma issue aberta foi encontrada na descrição do PR.",
        "",
        "Adicione uma linha como `Closes #12`, `Fixes #7` ou `Resolves #23` "
        "(em português ou inglês, `Closes`/`Fecha` não importa — use exatamente "
        "uma destas palavras: close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved).",
    ]
    if invalidas:
        linhas_erro.append(f"\nNúmeros citados que não existem como issue: {', '.join(f'#{n}' for n in invalidas)}.")
    if fechadas:
        linhas_erro.append(f"\nNúmeros citados que já estão fechados: {', '.join(f'#{n}' for n in fechadas)}.")

    corpo = (
        f"{MARCADOR}\n"
        "## ❌ Nenhuma issue vinculada\n\n"
        + "\n".join(linhas_erro)
        + "\n\n---\n*Edite a descrição do PR e o check reavalia automaticamente.*"
    )
    comentar(repo, pr_number, corpo)
    sys.exit(1)


if __name__ == "__main__":
    main()
