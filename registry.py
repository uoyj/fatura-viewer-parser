"""
Registry de parsers.

Mapeia (banco, versao) → função parse_fn.

Uso:
    from registry import get_parser
    parser = get_parser("sofisa", versao="latest")  # retorna a função parse_fn mais recente
    fatura = parser(extractor_output)

A versão "latest" resolve para a versão mais recente cadastrada para o banco.
"""

from __future__ import annotations

from typing import Callable

# Tipo da função parser: recebe o output do extractor (dict), retorna uma Fatura
ParseFn = Callable[[dict], "Fatura"]  # noqa: F821 — forward ref para schemas.Fatura

# Registry: (banco, versao) → parse_fn
_registry: dict[tuple[str, str], ParseFn] = {}


def registrar(banco: str, versao: str):
    """
    Decorator para registrar um parser.

    Exemplo:
        @registrar("sofisa", "2026-09")
        def parse_sofisa_2026_09(extractor_output: dict) -> Fatura:
            ...
    """
    def decorator(fn: ParseFn) -> ParseFn:
        _registry[(banco.lower(), versao)] = fn
        return fn
    return decorator


def get_parser(banco: str, versao: str = "latest") -> ParseFn:
    """
    Retorna a função parser para o (banco, versao) solicitado.

    - versao="latest": resolve para a versão mais recente cadastrada para o banco.
    - Se não encontrado, levanta ValueError com os bancos/versões disponíveis.
    """
    banco = banco.lower()

    if versao == "latest":
        # Buscar a versão mais recente para este banco
        versoes = [v for (b, v) in _registry.keys() if b == banco]
        if not versoes:
            _raise_lookup_error(banco, versao)
        # Assume formato semver YYYY-MM; ordena como string (ISO é lexicograficamente ordenável)
        versao = sorted(versoes, reverse=True)[0]

    key = (banco, versao)
    if key not in _registry:
        _raise_lookup_error(banco, versao)

    return _registry[key]


def _raise_lookup_error(banco: str, versao: str):
    """Levanta ValueError com informações de debug."""
    bancos_disponiveis = sorted({b for (b, _) in _registry.keys()})
    versoes_banco = sorted({v for (b, v) in _registry.keys() if b == banco}, reverse=True)

    msg = f"Parser não encontrado para (banco={banco!r}, versao={versao!r})."
    if versoes_banco:
        msg += f" Versões disponíveis para '{banco}': {versoes_banco}."
    else:
        msg += f" Bancos disponíveis: {bancos_disponiveis}."
    raise ValueError(msg)


def parsers_registrados() -> dict[str, list[str]]:
    """
    Lista todos os parsers registrados, agrupados por banco.
    Útil para debug/CLI.
    """
    result: dict[str, list[str]] = {}
    for (banco, versao) in _registry.keys():
        result.setdefault(banco, []).append(versao)
    for banco in result:
        result[banco] = sorted(result[banco], reverse=True)
    return result
