"""
Testes para parsers/nubank_2026_09.py — DUAS faturas do MESMO modelo.

Padrão da suíte: fixtures de módulo extraem/parseiam cada PDF UMA vez
(parametrizadas pelos 2 fatos); `test_golden` é a proteção forte e os demais
testes são asserções pontuais sobre a MESMA fatura já parseada.

Cobre:
- Header: datas, totais
- Soma das transações == débitos/créditos do header + equação
  saldo_anterior - creditos + debitos + outros = total_a_pagar
- Estrutura: datas ISO, valores Decimal-as-string, cartão tagueado,
  categorias nulas (passo posterior), parcelas coerentes, 2 cartões

Skip silencioso quando o PDF não existe (ex: CI sem in/).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from extractors.pdf_extractor import extract_text
from registry import get_parser
from schemas import fatura_para_dict

slow = pytest.mark.slow  # lê PDF do disco

PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR = PROJECT_ROOT / "in"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
TOL = Decimal("0.01")

# --- Ground truth das duas faturas ---
FACTS = {
    "a": {
        "pdf": "Nubank_2026-09-09.pdf",
        "fixture": "nubank_2026_09_a.expected.json",
        "fechamento": "2026-09-02",
        "vencimento": "2026-09-09",
        "total_a_pagar": "231.26",
        "pagamento_minimo": "34.68",
        "saldo_anterior": Decimal("194.75"),
        "creditos": Decimal("194.75"),
        "debitos": Decimal("231.26"),
        "outros": Decimal("0"),
        "cartoes": ["·3528", "·8188"],
    },
    "b": {
        "pdf": "Nubank_2026-09-22.pdf",
        "fixture": "nubank_2026_09_b.expected.json",
        "fechamento": "2026-09-15",
        "vencimento": "2026-09-22",
        "total_a_pagar": "1577.25",
        "pagamento_minimo": "236.58",
        "saldo_anterior": Decimal("2720.43"),
        "creditos": Decimal("2720.43"),
        "debitos": Decimal("1445.34"),
        "outros": Decimal("131.91"),
        "cartoes": ["·5065", "·5118"],
    },
}


# --- Fixtures de módulo (parametrizadas pelas 2 faturas) -------------------

@pytest.fixture(scope="module", params=list(FACTS), ids=list(FACTS))
def fact(request):
    return FACTS[request.param]


@pytest.fixture(scope="module")
def fatura(fact):
    pdf_path = PDF_DIR / fact["pdf"]
    if not pdf_path.exists():
        pytest.skip(f"PDF não encontrado em {pdf_path}")
    return get_parser("nubank", versao="2026-09")(extract_text(pdf_path))


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
def test_header_equacao_e_estrutura(fatura, fatura_dict, fact):
    """Header, equação do resumo, contrato do schema e cartões distintos."""
    assert fatura.fechamento.isoformat() == fact["fechamento"]
    assert fatura.vencimento.isoformat() == fact["vencimento"]
    assert str(fatura.total_a_pagar) == fact["total_a_pagar"]
    assert str(fatura.pagamento_minimo) == fact["pagamento_minimo"]

    assert len(fatura.transacoes) > 0
    debitos = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
    creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
    assert abs(debitos - fact["debitos"]) <= TOL
    assert abs(creditos - fact["creditos"]) <= TOL

    esperado = (
        fact["saldo_anterior"] - fact["creditos"] + fact["debitos"] + fact["outros"]
    )
    assert abs(esperado - Decimal(str(fatura.total_a_pagar))) <= TOL

    cartoes = set()
    for t in fatura_dict["transacoes"]:
        assert isinstance(t["valor"], str)
        Decimal(t["valor"])  # não deve lançar
        datetime.fromisoformat(t["data"])
        assert isinstance(t["cartao"], str)
        assert t["categoria"] is None  # categorização é passo posterior
        if t["cartao"]:
            cartoes.add(t["cartao"])
        if t["parcela_atual"] is not None:
            assert t["parcela_total"] is not None
            assert t["parcela_atual"] <= t["parcela_total"]

    assert sorted(cartoes) == fact["cartoes"]
