"""
Testes para parsers/nubank_2026_09.py.

Valida:
- Header: fechamento, vencimento, total_a_pagar, pagamento_minimo
- Soma global de transacoes bate com debitos e creditos do header
- Equacao: saldo_anterior - creditos + debitos + outros = total_a_pagar
- Estrutura individual de transacoes (datas ISO, valores string, cartao, parcelas)

Cobre DUAS faturas, parametrizadas:
- Nubank_2026-09-09.pdf (Ricioli, fixture _a)
- Nubank_2026-09-22.pdf (Gabriel, fixture _b)

Os testes usam skipif para pular silenciosamente quando o PDF nao existe.
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

# --- Caminhos ---
PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR = PROJECT_ROOT / "in"


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

# Resolve paths e flag de skip por fatura
for key in FACTS:
    pdf_path = PDF_DIR / FACTS[key]["pdf"]
    FACTS[key]["pdf_path"] = pdf_path
    FACTS[key]["tem_pdf"] = pdf_path.exists()
    FACTS[key]["fixture_path"] = PROJECT_ROOT / "tests" / "fixtures" / FACTS[key]["fixture"]


# ---------------------------------------------------------------------------
# Fixtures por fatura (module scope — extrai PDF e parse uma vez por fatura)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def extractor_output_09():
    """Extract do PDF 09SET — skip se nao existe."""
    pdf_path = FACTS["a"]["pdf_path"]
    if not pdf_path.exists():
        pytest.skip(f"PDF nao encontrado em {pdf_path}")
    return extract_text(pdf_path)


@pytest.fixture(scope="module")
def extractor_output_22():
    """Extract do PDF 22SET — skip se nao existe."""
    pdf_path = FACTS["b"]["pdf_path"]
    if not pdf_path.exists():
        pytest.skip(f"PDF nao encontrado em {pdf_path}")
    return extract_text(pdf_path)


@pytest.fixture(scope="module")
def fatura_09(extractor_output_09):
    parse_fn = get_parser("nubank", versao="2026-09")
    return parse_fn(extractor_output_09)


@pytest.fixture(scope="module")
def fatura_22(extractor_output_22):
    parse_fn = get_parser("nubank", versao="2026-09")
    return parse_fn(extractor_output_22)


@pytest.fixture(scope="module")
def fatura_dict_09(fatura_09):
    return fatura_para_dict(fatura_09)


@pytest.fixture(scope="module")
def fatura_dict_22(fatura_22):
    return fatura_para_dict(fatura_22)


@pytest.fixture(scope="module")
def expected_json_09():
    with open(FACTS["a"]["fixture_path"]) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def expected_json_22():
    with open(FACTS["b"]["fixture_path"]) as f:
        return json.load(f)


# ===========================================================================
# HEADER
# ===========================================================================

class TestHeader09:
    def test_fechamento(self, fatura_09):
        assert fatura_09.fechamento.isoformat() == FACTS["a"]["fechamento"]

    def test_vencimento(self, fatura_09):
        assert fatura_09.vencimento.isoformat() == FACTS["a"]["vencimento"]

    def test_total_a_pagar(self, fatura_09):
        assert str(fatura_09.total_a_pagar) == FACTS["a"]["total_a_pagar"]

    def test_pagamento_minimo(self, fatura_09):
        assert str(fatura_09.pagamento_minimo) == FACTS["a"]["pagamento_minimo"]


class TestHeader22:
    def test_fechamento(self, fatura_22):
        assert fatura_22.fechamento.isoformat() == FACTS["b"]["fechamento"]

    def test_vencimento(self, fatura_22):
        assert fatura_22.vencimento.isoformat() == FACTS["b"]["vencimento"]

    def test_total_a_pagar(self, fatura_22):
        assert str(fatura_22.total_a_pagar) == FACTS["b"]["total_a_pagar"]

    def test_pagamento_minimo(self, fatura_22):
        assert str(fatura_22.pagamento_minimo) == FACTS["b"]["pagamento_minimo"]


# ===========================================================================
# SOMAS / EQUACAO DE VALIDACAO
# ===========================================================================

class TestSomas09:
    def test_soma_debitos(self, fatura_09):
        debitos = sum((t.valor for t in fatura_09.transacoes if t.valor > 0), Decimal(0))
        assert abs(debitos - FACTS["a"]["debitos"]) <= Decimal("0.01")

    def test_soma_creditos(self, fatura_09):
        creditos = -sum((t.valor for t in fatura_09.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - FACTS["a"]["creditos"]) <= Decimal("0.01")

    def test_equacao_header_fecha(self, fatura_09):
        f = FACTS["a"]
        esperado = f["saldo_anterior"] - f["creditos"] + f["debitos"] + f["outros"]
        assert abs(esperado - Decimal(str(fatura_09.total_a_pagar))) <= Decimal("0.01")

    def test_transacoes_nao_vazias(self, fatura_09):
        assert len(fatura_09.transacoes) > 0


class TestSomas22:
    def test_soma_debitos(self, fatura_22):
        debitos = sum((t.valor for t in fatura_22.transacoes if t.valor > 0), Decimal(0))
        assert abs(debitos - FACTS["b"]["debitos"]) <= Decimal("0.01")

    def test_soma_creditos(self, fatura_22):
        creditos = -sum((t.valor for t in fatura_22.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - FACTS["b"]["creditos"]) <= Decimal("0.01")

    def test_equacao_header_fecha(self, fatura_22):
        f = FACTS["b"]
        esperado = f["saldo_anterior"] - f["creditos"] + f["debitos"] + f["outros"]
        assert abs(esperado - Decimal(str(fatura_22.total_a_pagar))) <= Decimal("0.01")

    def test_transacoes_nao_vazias(self, fatura_22):
        assert len(fatura_22.transacoes) > 0


# ===========================================================================
# ESTRUTURA DE TRANSACOES
# ===========================================================================

class TestTransacoes09:
    def test_valores_decimal_string(self, fatura_dict_09):
        for t in fatura_dict_09["transacoes"]:
            assert isinstance(t["valor"], str)
            Decimal(t["valor"])

    def test_datas_iso(self, fatura_dict_09):
        for t in fatura_dict_09["transacoes"]:
            datetime.fromisoformat(t["data"])

    def test_cartao_tagueado(self, fatura_dict_09):
        for t in fatura_dict_09["transacoes"]:
            assert "cartao" in t
            assert isinstance(t["cartao"], str)

    def test_sem_categoria(self, fatura_dict_09):
        for t in fatura_dict_09["transacoes"]:
            assert t["categoria"] is None

    def test_parcela_atual_menor_ou_igual_total(self, fatura_dict_09):
        for t in fatura_dict_09["transacoes"]:
            if t["parcela_atual"] is not None:
                assert t["parcela_total"] is not None
                assert t["parcela_atual"] <= t["parcela_total"]

    def test_cartoes_distintos(self, fatura_dict_09):
        cartoes = sorted({t["cartao"] for t in fatura_dict_09["transacoes"] if t["cartao"]})
        assert cartoes == FACTS["a"]["cartoes"]


class TestTransacoes22:
    def test_valores_decimal_string(self, fatura_dict_22):
        for t in fatura_dict_22["transacoes"]:
            assert isinstance(t["valor"], str)
            Decimal(t["valor"])

    def test_datas_iso(self, fatura_dict_22):
        for t in fatura_dict_22["transacoes"]:
            datetime.fromisoformat(t["data"])

    def test_cartao_tagueado(self, fatura_dict_22):
        for t in fatura_dict_22["transacoes"]:
            assert "cartao" in t
            assert isinstance(t["cartao"], str)

    def test_sem_categoria(self, fatura_dict_22):
        for t in fatura_dict_22["transacoes"]:
            assert t["categoria"] is None

    def test_parcela_atual_menor_ou_igual_total(self, fatura_dict_22):
        for t in fatura_dict_22["transacoes"]:
            if t["parcela_atual"] is not None:
                assert t["parcela_total"] is not None
                assert t["parcela_atual"] <= t["parcela_total"]

    def test_cartoes_distintos(self, fatura_dict_22):
        cartoes = sorted({t["cartao"] for t in fatura_dict_22["transacoes"] if t["cartao"]})
        assert cartoes == FACTS["b"]["cartoes"]


# ===========================================================================
# CONSISTENCIA JSON
# ===========================================================================

class TestJSONConsistency09:
    def test_json_equivalente(self, fatura_dict_09, expected_json_09):
        assert fatura_dict_09 == expected_json_09


class TestJSONConsistency22:
    def test_json_equivalente(self, fatura_dict_22, expected_json_22):
        assert fatura_dict_22 == expected_json_22
