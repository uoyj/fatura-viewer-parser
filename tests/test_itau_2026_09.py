"""
Testes para parsers/itau_2026_09.py — DUAS faturas do MESMO modelo.

Padrão da suíte: fixtures de módulo extraem/parseiam cada PDF UMA vez
(parametrizadas pelos 2 fatos) e os testes reusam o resultado.
`test_golden` é a proteção forte; o resto são asserções pontuais sobre a
MESMA fatura já parseada.

Cobre:
- Header: datas, totais, saldo_anterior, créditos
- ENCARGOS como campo próprio do header (default 0) e a equação
  saldo_anterior - creditos + debitos + encargos = total_a_pagar
- Soma das transações == débitos; encargos NÃO entram na soma
- PARCELA `NN/MM` colada no estabelecimento ("nuuvem *N 02/06" = 2 de 6)
- Limpeza do sufixo "categoria CIDADE" na descrição
- Estrutura: datas ISO, valores Decimal-as-string, cartão tagueado, ids

Skip silencioso quando o PDF não existe (ex: CI sem in/).

ATENÇÃO: mudou o output de fatura_para_dict → regenere os fixtures rodando o
parse real nos PDFs de in/ (nunca edite o .expected.json à mão).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from extractors.pdf_extractor import extract_text
from parsers.itau_2026_09 import (
    _extrair_parcela,
    _extract_header,
    _limpar_categoria_cidade,
)
from registry import get_parser
from schemas import fatura_para_dict

PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR = PROJECT_ROOT / "in"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
TOL = Decimal("0.01")

slow = pytest.mark.slow  # lê PDF do disco

# --- Ground truth das duas faturas ---
FACTS = {
    "a": {
        "pdf": "Fatura_Itau_20260917-204150.pdf",
        "fixture": "itau_2026_09.expected.json",
        "fechamento": "2026-09-03",
        "vencimento": "2026-09-10",
        "total_a_pagar": "1940.84",
        "pagamento_minimo": "194.08",
        "saldo_anterior": Decimal("2111.00"),
        "creditos": Decimal("2111.00"),
        "debitos": Decimal("1940.84"),
        "encargos": Decimal("0"),
        "cartao": "3620",
        # parcela NN/MM embutida no estabelecimento
        "parcelas": {
            "FARMACIA DRO*N": (6, 6),
            "JIM.COM* IBUY": (3, 12),
            "FILIAL 523 CTB": (2, 3),
        },
    },
    "b": {
        "pdf": "Fatura_Itau_20260918-151358.pdf",
        "fixture": "itau_2026_09_b.expected.json",
        "fechamento": "2026-09-08",
        "vencimento": "2026-09-15",
        "total_a_pagar": "990.26",
        "pagamento_minimo": "202.77",
        "saldo_anterior": Decimal("2342.49"),
        "creditos": Decimal("2342.49"),
        "debitos": Decimal("874.99"),
        "encargos": Decimal("115.27"),
        "cartao": "3613",
        "parcelas": {
            "nuuvem *N": (2, 6),
        },
    },
}


# --- Fixtures de módulo (parametrizadas pelas 2 faturas) -------------------

@pytest.fixture(scope="module", params=list(FACTS), ids=list(FACTS))
def fact(request):
    return FACTS[request.param]


@pytest.fixture(scope="module")
def extractor_output(fact):
    pdf_path = PDF_DIR / fact["pdf"]
    if not pdf_path.exists():
        pytest.skip(f"PDF não encontrado em {pdf_path}")
    return extract_text(pdf_path)


@pytest.fixture(scope="module")
def fatura(extractor_output):
    """Reusa o extract do módulo — o PDF é extraído UMA vez por fatura."""
    return get_parser("itau", "2026-09")(extractor_output)


@pytest.fixture(scope="module")
def header(extractor_output):
    return _extract_header(extractor_output["pages"])


@pytest.fixture(scope="module")
def fatura_dict(fatura):
    return fatura_para_dict(fatura)


@pytest.fixture(scope="module")
def expected_json(fact):
    with open(FIXTURES_DIR / fact["fixture"]) as f:
        return json.load(f)


# --- Testes ---------------------------------------------------------------

@slow
def test_golden(fatura_dict, expected_json):
    """Proteção principal: igualdade exata com o fixture aprovado."""
    assert fatura_dict == expected_json


@slow
def test_header_e_somas(fatura, header, fact):
    """
    Header (datas, totais, saldo, créditos, encargos, débitos) + equação e soma
    das transações. Encargos ficam FORA da soma das transações.
    """
    assert fatura.fechamento.isoformat() == fact["fechamento"]
    assert fatura.vencimento.isoformat() == fact["vencimento"]
    assert str(fatura.total_a_pagar) == fact["total_a_pagar"]
    assert str(fatura.pagamento_minimo) == fact["pagamento_minimo"]
    assert header["saldo_anterior"] == fact["saldo_anterior"]
    assert header["creditos"] == fact["creditos"]
    assert header["encargos"] == fact["encargos"]
    assert header["debitos"] == fact["debitos"]

    esperado = (
        header["saldo_anterior"] - header["creditos"]
        + header["debitos"] + header["encargos"]
    )
    assert abs(esperado - fatura.total_a_pagar) <= TOL
    assert esperado == Decimal(fact["total_a_pagar"])

    assert len(fatura.transacoes) > 0
    soma = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
    creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
    assert abs(soma - fact["debitos"]) <= TOL
    assert abs(creditos - fact["creditos"]) <= TOL
    if fact["encargos"]:
        assert soma != fact["debitos"] + fact["encargos"]


@slow
def test_estrutura_e_parcelas(fatura, fatura_dict, fact):
    """Schema + parcelas NN/MM extraídas da descrição e removidas dela."""
    transacoes = fatura_dict["transacoes"]

    ids = [t["id"] for t in transacoes]
    assert ids == list(range(len(ids)))

    mapa = {}
    for t, t_raw in zip(transacoes, fatura.transacoes):
        assert isinstance(t["valor"], str)
        Decimal(t["valor"])  # não deve lançar
        datetime.fromisoformat(t["data"])
        assert t["cartao"] == fact["cartao"]
        assert t["categoria"] is None  # categorização é passo posterior
        # o token de parcela sai da descrição
        assert not re.search(r"\d{2}/\d{2}", t_raw.descricao), t_raw.descricao
        if t["parcela_atual"] is not None:
            assert t["parcela_total"] is not None
            assert t["parcela_atual"] <= t["parcela_total"]
            mapa[t_raw.descricao] = (t["parcela_atual"], t["parcela_total"])

    for descricao, parcela in fact["parcelas"].items():
        assert descricao in mapa, f"{descricao!r} ausente (tem: {sorted(mapa)})"
        assert mapa[descricao] == parcela


def test_extrair_parcela():
    """Helpers puros (não dependem do PDF): todos os casos, um loop."""
    casos = [
        ("nuuvem *N 02/06", (2, 6)),
        ("FARMACIA DRO*N06/06", (6, 6)),
        ("JIM.COM* IBUY03/12", (3, 12)),
        ("FILIAL523CTB02/03", (2, 3)),
        ("AMAZONMKTPLC*M 03/10", (3, 10)),
        ("LOJA SEM PARCELA", (None, None)),
        ("PAGTO 13/08 LOJA", (None, None)),  # token com cara de DATA não é parcela
    ]
    for texto, esperado in casos:
        assert _extrair_parcela(texto) == esperado, f"falhou em {texto!r}"


def test_limpar_categoria_cidade():
    """Helpers puros (não dependem do PDF): todos os casos, um loop."""
    casos = [
        ("LANCHONETE DOIS CORACCU supermercado CURITIBA", "LANCHONETE DOIS CORACCU"),
        # não remove quando o estabelecimento JÁ começa com a categoria
        ("LANCHONETE DOIS CORACCU", "LANCHONETE DOIS CORACCU"),
        # até dois sufixos (categoria + cidade)
        ("LOJA X transporte SAO PAULO supermercado CURITIBA", "LOJA X"),
        # descrição sem sufixo fica intacta
        ("AMAZON BR", "AMAZON BR"),
        ("FARMACIA DRO*N", "FARMACIA DRO*N"),
        ("Compra a Vista NONO CAFE", "Compra a Vista NONO CAFE"),
    ]
    for texto, esperado in casos:
        assert _limpar_categoria_cidade(texto) == esperado, f"falhou em {texto!r}"
