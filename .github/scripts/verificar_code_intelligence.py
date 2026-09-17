#!/usr/bin/env python3
"""Roda `code-intelligence diff` (base do PR vs. HEAD) e comenta o resultado no PR.

O código-fonte da ferramenta vive em `.github/code_intelligence/` (cópia
vendorizada, somente leitura — nunca edite esses arquivos aqui). Sai com
código 2 se o PR introduz qualquer violação nova em relação à `main`
(qualquer severidade — a política do professor é tolerância zero), senão 0.
"""
import json
import os
import subprocess
import sys

MARCADOR = "<!-- code-intelligence-bot -->"


def rodar_diff():
    base_sha = os.environ["BASE_SHA"]
    cli = os.path.join(".github", "code_intelligence", "code-intelligence.py")
    return subprocess.run(
        [sys.executable, cli, "diff", base_sha, "--root", "."],
        capture_output=True, text=True,
    )


def formatar(payload):
    regressoes = payload.get("regressions", [])
    if not regressoes:
        return (
            f"{MARCADOR}\n"
            "## ✅ Code Intelligence — nenhuma violação nova\n\n"
            "Este PR não introduz nenhuma violação de qualidade de código em relação à `main`."
        )
    linhas = [
        f"- **[{v['severity']}] {v['code']}** em `{v['file']}:{v['line']}` — {v['message']}"
        for v in regressoes
    ]
    return (
        f"{MARCADOR}\n"
        "## ❌ Code Intelligence — violações novas detectadas\n\n"
        f"Este PR introduz {len(regressoes)} violação(ões) de qualidade que não existiam na `main`:\n\n"
        + "\n".join(linhas)
        + "\n\n---\n*Verificação automática (naming, docstrings, tamanho, duplicação, etc). "
          "Corrija os pontos acima — um novo push reavalia automaticamente.*"
    )


def comentar(corpo):
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
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


def main():
    resultado = rodar_diff()
    try:
        payload = json.loads(resultado.stdout)
    except json.JSONDecodeError:
        print("erro: saída inesperada do code-intelligence", file=sys.stderr)
        print("stdout:", resultado.stdout, file=sys.stderr)
        print("stderr:", resultado.stderr, file=sys.stderr)
        sys.exit(1)

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    corpo = formatar(payload)
    comentar(corpo)
    sys.exit(2 if payload.get("regressions") else 0)


if __name__ == "__main__":
    main()
