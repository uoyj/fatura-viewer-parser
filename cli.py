"""
CLI para fatura-viewer.

Uso:
    python cli.py parse <pdf_path> [--banco BANCO] [--versao VERSAO]

Exemplos:
    python cli.py parse faturas/sofisa_setembro_2026.pdf --banco sofisa
    python cli.py parse faturas/exemplo.pdf --banco sofisa --versao 2026-09
    python cli.py parsers  # lista todos os parsers registrados
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Importar para registrar todos os parsers
from parsers import *  # noqa: F401, F403 — side-effect: registra parsers
from registry import get_parser, parsers_registrados
from schemas import fatura_para_dict
from extractors.pdf_extractor import extract_text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="fatura-viewer: extrai e parseia faturas de cartão de crédito",
    )
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponíveis")

    # Subcomando: parse
    parse_parser = subparsers.add_parser("parse", help="Parseia um PDF de fatura")
    parse_parser.add_argument("pdf_path", type=str, help="Caminho do PDF da fatura")
    parse_parser.add_argument("--banco", type=str, required=True, help="Nome do banco (ex: sofisa)")
    parse_parser.add_argument("--versao", type=str, default="latest", help="Versão do layout (default: latest)")
    parse_parser.add_argument("--categorizar", action="store_true", help="Aplicar categorização por palavra-chave")

    # Subcomando: parsers
    subparsers.add_parser("parsers", help="Lista todos os parsers registrados")

    # Subcomando: extract (debug)
    extract_parser = subparsers.add_parser("extract", help="Extrai texto bruto do PDF (debug)")
    extract_parser.add_argument("pdf_path", type=str, help="Caminho do PDF")
    extract_parser.add_argument("--compact", action="store_true", help="Saída compacta")

    args = parser.parse_args()

    if args.command == "parsers":
        return _cmd_parsers()

    if args.command == "parse":
        return _cmd_parse(args)

    if args.command == "extract":
        return _cmd_extract(args)

    parser.print_help()
    return 1


def _cmd_parse(args: argparse.Namespace) -> int:
    """Roda o parser sobre um PDF e imprime o JSON resultante."""
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"❌ Erro: arquivo não encontrado: {pdf_path}", file=sys.stderr)
        return 1

    try:
        # 1. Extrair texto do PDF
        extractor_output = extract_text(pdf_path)

        # 2. Obter o parser do registry
        parse_fn = get_parser(args.banco, args.versao)

        # 3. Parser parseia
        fatura = parse_fn(extractor_output)

        # 4. Categorizar (opcional)
        if args.categorizar:
            from categorizer import categorizar
            fatura = categorizar(fatura)

        # 5. Serializar para JSON
        output = fatura_para_dict(fatura)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    except Exception as e:
        print(f"❌ Erro: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


def _cmd_parsers() -> int:
    """Lista todos os parsers registrados."""
    parsers = parsers_registrados()
    if not parsers:
        print("Nenhum parser registrado.")
        return 0

    print("Parsers registrados:")
    for banco, versoes in sorted(parsers.items()):
        print(f"  {banco}: {', '.join(versoes)}")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    """Extrai texto bruto do PDF para debug."""
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"❌ Erquivo não encontrado: {pdf_path}", file=sys.stderr)
        return 1

    data = extract_text(pdf_path)
    if args.compact:
        # Apenas texto concatenado
        text = "\n".join(
            line["text"] for page in data["pages"] for line in page["lines"]
        )
        print(text)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
