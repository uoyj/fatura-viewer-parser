"""
Inferência de banco pela fatura — marcadores de texto (pura, testável).

Não é fingerprint perfeito: é uma tabela de marcadores que evolui com os
bancos. Match em texto normalizado (sem acentos, upper), só no começo do
documento (barato e suficiente — a marca está no header).
"""

from __future__ import annotations

import unicodedata

# Marcadores por banco (serão normalizados: sem acento + upper).
# Ordem das chaves NÃO importa; ordem dos marcadores sim (mais específico primeiro).
MARCADORES: dict[str, list[str]] = {
    "sofisa": ["SOFISA DIRETO", "SOFISA"],
    "nubank": ["COMUNIDADE.NU", "NUBANK"],
    "itau": ["ITAU UNIBANCO", "ITAU"],
    "mercadopago": ["MERCADO PAGO"],
}

# Só o começo do texto: a marca está no header/cabeçalho
JANELA_CHARS = 4000


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).upper()


def inferir_banco(texto: str, bancos: list[str]) -> str | None:
    """
    Retorna o nome do banco (deve estar em `bancos`, que vêm do registry)
    ou None. `bancos` limita a busca ao que o app realmente suporta.
    """
    t = _norm(texto[:JANELA_CHARS])
    for banco in bancos:
        for marcador in MARCADORES.get(banco, []):
            if _norm(marcador) in t:
                return banco
    return None
