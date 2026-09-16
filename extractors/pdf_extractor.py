"""
Extractor genérico de PDFs de faturas.

DUAS funções públicas:

1. extract_text(path: Path) -> dict
   Estrutura:
   {
       "pages": [
           {
               "page_number": int,
               "text": str,          # texto bruto concatenado da página
               "lines": [            # linhas extraídas (top → bottom)
                   {"text": str, "top": float, "x0": float,
                    "words": [{"text": str, "x0": float, "x1": float, "top": float}]}
               ],
               "tables": [...]       # tabelas extraídas (lista de listas de células)
           }
       ]
   }

   Usa pdfplumber como método primário. Se uma página tiver texto vazio
   (PDF scan / imagem), faz fallback para pymupdf para extrair palavras
   posicionadas.

2. extract_lines(path: Path) -> list[dict]
   Uma lista achatada de linhas visuais do PDF:
   {
       "page": int,
       "top": float,
       "words": [{"text": str, "x0": float}]  # ordenadas por x0
   }

REGRA DE OURO: este módulo NÃO contém lógica de interpretção de fatura.
Só extrai posições e texto. Os parsers fazem a interpretação.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pymupdf  # pymupdf (API atual, substitui `fitz`)
import pdfplumber

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Função principal pública 1: extract_text
# ---------------------------------------------------------------------------

def extract_text(path: Path) -> dict[str, Any]:
    """
    Extrai texto e estrutura bruta de um PDF.

    Estratégia:
    1. Tenta pdfplumber (melhor preservação de posição).
    2. Se alguma página tem texto vazio (scan), faz fallback para pymupdf
       apenas nessa página para extrair palavras posicionadas.

    Returns:
        dict com chave "pages" → lista de dicts por página.
    """
    path = Path(path)
    result: dict[str, Any] = {"pages": []}

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            page_data = _extract_page_with_pdfplumber(page, page_num)

            # Fallback para pymupdf se pdfplumber não conseguiu texto
            if not page_data["text"].strip():
                logger.info(
                    "Página %d sem texto via pdfplumber. Fallback para pymupdf.",
                    page_num,
                )
                page_data = _extract_page_with_pymupdf(path, page_num, page_data)

            result["pages"].append(page_data)

    return result


def _extract_page_with_pdfplumber(
    page: Any, page_num: int
) -> dict[str, Any]:
    """Extrai dados de uma página usando pdfplumber."""
    text: str = page.extract_text() or ""
    lines: list[dict] = []

    # Extrai linhas: cada "line" é uma lista de palavras
    raw_lines = page.extract_text(layout=True) or ""
    # Também extrai palavras para posicionamento preciso
    words = page.extract_words() or []

    # Agrupar palavras em linhas por proximidade de 'top'
    lines_dict: dict[float, list[dict]] = {}
    for w in words:
        # Arredondar 'top' para agrupar palavras da mesma linha
        key = round(w.get("top", 0), 1)
        lines_dict.setdefault(key, []).append({
            "text": w.get("text", ""),
            "x0": round(w.get("x0", 0), 2),
            "x1": round(w.get("x1", 0), 2),
            "top": round(w.get("top", 0), 2),
        })

    # Ordenar linhas por 'top'
    for top in sorted(lines_dict.keys()):
        word_list = sorted(lines_dict[top], key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in word_list)
        lines.append({
            "text": line_text,
            "top": top,
            "x0": word_list[0]["x0"] if word_list else round(0, 2),  # início da linha
            "words": word_list,
        })

    # Tabelas (extração básica)
    tables = page.extract_tables() or []

    return {
        "page_number": page_num,
        "text": text,
        "lines": lines,
        "tables": tables,
    }


def _extract_page_with_pymupdf(
    path: Path, page_num: int, page_data: dict
) -> dict[str, Any]:
    """
    Fallback: usa pymupdf para extrair palavras posicionadas de uma página.

    Só é chamado quando pdfplumber não encontra texto (PDF scan).
    Extrai posições das palavras — não faz OCR (imagem → texto exigiu
    integração externa de OCR; aí o extractor só fornece posições).
    """
    doc = pymupdf.open(path)
    page = doc.load_page(page_num - 1)  # 0-indexed

    words = page.get_text("words")  # lista de (x0, y0, x1, y1, "palavra", block_no, line_no, word_no)
    doc.close()

    lines_dict: dict[float, list[dict]] = {}
    for w in words:
        x0, y0, x1, y1, word_str = w[0], w[1], w[2], w[3], w[4]
        key = round(y0, 1)  # 'top' em pymupdf é y0
        lines_dict.setdefault(key, []).append({
            "text": word_str,
            "x0": round(x0, 2),
            "x1": round(x1, 2),
            "top": round(y0, 2),
        })

    lines: list[dict] = []
    for top in sorted(lines_dict.keys()):
        word_list = sorted(lines_dict[top], key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in word_list)
        lines.append({
            "text": line_text,
            "top": top,
            "x0": word_list[0]["x0"] if word_list else round(0, 2),
            "words": word_list,
        })

    text = "\n".join(line["text"] for line in lines)

    return {
        "page_number": page_num,
        "text": text,
        "lines": lines,
        "tables": page_data.get("tables", []),  # mantém mesmo se houver
    }


# ---------------------------------------------------------------------------
# Função pública 2: extract_lines
# ---------------------------------------------------------------------------

def extract_lines(path: Path) -> list[dict]:
    """
    Extrai uma lista achatada de linhas visuais do PDF.

    Cada elemento:
    {
        "page": int,
        "top": float,
        "words": [{"text": str, "x0": float}]  # ordenadas por x0
    }

    Útil para parsers que precisam percorrer linha por linha sem
    carregar todo o texto em memória.
    """
    full = extract_text(path)
    result: list[dict] = []

    for page in full["pages"]:
        page_num = page["page_number"]
        for line in page["lines"]:
            result.append({
                "page": page_num,
                "top": line["top"],
                "words": [
                    {"text": w["text"], "x0": w["x0"]}
                    for w in line["words"]
                ],
            })

    return result
