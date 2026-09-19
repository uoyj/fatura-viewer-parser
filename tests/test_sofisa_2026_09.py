"""
Testes para parsers/sofisa_2026_09.py.

Padrão da suíte: UMA fixture de módulo extrai/parseia o PDF uma única vez e
todos os testes do módulo reusam o resultado. O `test_golden` é a proteção
forte (igualdade exata com tests/fixtures/*.expected.json); os demais testes
são asserções pontuais sobre a MESMA fatura já parseada.

Valida:
- Header: fechamento, vencimento, total_a_pagar, pagamento_minimo
- Somas globais == débitos/créditos do header + equação que fecha
- Estrutura: datas ISO, valores Decimal-as-string, cartão tagueado,
  categorias nulas (categorização é passo posterior), parcelas coerentes,
  96 transações em 3 cartões
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from registry import get_parser
from schemas import fatura_para_dict

pytestmark = pytest.mark.slow  # lê PDF do disco

PROJECT_ROOT = Path(__file__).parent.parent
PDF_PATH = PROJECT_ROOT / "in" / "Fatura.pdf"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "sofisa_2026_09.expected.json"

# Ground truth (header)
SALDO_ANTERIOR = Decimal("4967.42")
CREDITOS = Decimal("5074.81")
DEBITOS = Decimal("6834.94")
TOL = Decimal("0.01")


# --- Fixtures de módulo: extrai e parseia UMA vez -------------------------

@pytest.fixture(scope="module")
def fatura(extract_cached):
    if not PDF_PATH.exists():
        pytest.skip(f"PDF não encontrado em {PDF_PATH}")
    return get_parser("sofisa", versao="2026-09")(extract_cached(PDF_PATH))


@pytest.fixture(scope="module")
def fatura_dict(fatura):
    return fatura_para_dict(fatura)


@pytest.fixture(scope="module")
def expected_json():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# --- Testes ---------------------------------------------------------------

def test_golden(fatura_dict, expected_json):
    """Proteção principal: o JSON do parser é idêntico ao fixture aprovado."""
    assert fatura_dict == expected_json


def test_header_e_equacao(fatura):
    """Datas/totais do header + somas que fecham a equação (tolerância R$0.01)."""
    assert fatura.fechamento.isoformat() == "2026-09-15"
    assert fatura.vencimento.isoformat() == "2026-09-20"
    assert fatura.total_a_pagar == Decimal("6727.55")
    assert fatura.pagamento_minimo == Decimal("672.76")

    assert len(fatura.transacoes) > 0
    debitos = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
    creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
    assert abs(debitos - DEBITOS) <= TOL
    assert abs(creditos - CREDITOS) <= TOL
    assert abs(SALDO_ANTERIOR - CREDITOS + DEBITOS - fatura.total_a_pagar) <= TOL


def test_estrutura(fatura, fatura_dict):
    """Contrato do schema: datas ISO, valores string, cartão tagueado, parcelas."""
    transacoes = fatura_dict["transacoes"]
    assert len(transacoes) == 96

    cartoes = set()
    for t in transacoes:
        assert isinstance(t["valor"], str)
        Decimal(t["valor"])  # não deve lançar
        datetime.fromisoformat(t["data"])
        assert isinstance(t["cartao"], str)
        assert t["categoria"] is None  # categorização é passo posterior
        cartoes.add(t["cartao"])
        if t["parcela_atual"] is not None:
            assert t["parcela_total"] is not None
            assert t["parcela_atual"] <= t["parcela_total"]

    assert sorted(cartoes) == [
        "4563**.******.0854", "4563**.******.9219", "4563**.******.9735",
    ]
