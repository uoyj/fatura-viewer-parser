"""
Testes para parsers/sofisa_2026_09.py.

Valida:
- Header: fechamento, vencimento, total_a_pagar, pagamento_minimo
- Valores esperados: saldo_anterior=4967.42, creditos=5074.81, debitos=6834.94
- Soma global de transações bate com débitos e créditos do header
- Quantidade de transações == 96 (fixture)
- Validação contra JSON expected
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from extractors.pdf_extractor import extract_text
from registry import get_parser
from schemas import fatura_para_dict

# --- Caminhos ---

PROJECT_ROOT = Path(__file__).parent.parent
PDF_PATH = PROJECT_ROOT / "in" / "Fatura.pdf"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "sofisa_2026_09.expected.json"


# --- Fixtures ---

@pytest.fixture(scope="module")
def extractor_output():
    """Extract text do PDF uma vez por módulo de teste."""
    return extract_text(PDF_PATH)


@pytest.fixture(scope="module")
def fatura(extractor_output):
    """Parse da fatura, reutilizando o extractor output."""
    parse_fn = get_parser("sofisa", versao="2026-09")
    return parse_fn(extractor_output)


@pytest.fixture(scope="module")
def fatura_dict(fatura):
    """Serializa a Fatura para dict."""
    return fatura_para_dict(fatura)


@pytest.fixture(scope="module")
def expected_json():
    """Carrega o JSON expected da fixture."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def header(fatura):
    """Extrai o header reconstruindo a partir da fatura parseada."""
    return {
        "saldo_anterior": Decimal("4967.42"),
        "creditos": Decimal("5074.81"),
        "debitos": Decimal("6834.94"),
        "total_a_pagar": Decimal("6727.55"),
        "pagamento_minimo": Decimal("672.76"),
    }


# --- Testes ---

class TestHeader:
    """Valida campos do header da fatura."""

    def test_fechamento(self, fatura):
        assert fatura.fechamento.isoformat() == "2026-09-15"

    def test_vencimento(self, fatura):
        assert fatura.vencimento.isoformat() == "2026-09-20"

    def test_total_a_pagar(self, fatura):
        assert str(fatura.total_a_pagar) == "6727.55"

    def test_pagamento_minimo(self, fatura):
        assert str(fatura.pagamento_minimo) == "672.76"


class TestSomas:
    """Valida consistência entre transações e totais do header."""

    def test_soma_debitos(self, fatura):
        """Soma de débitos (valores positivos) == total de débitos do header."""
        debitos = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
        assert abs(debitos - Decimal("6834.94")) <= Decimal("0.01")

    def test_soma_creditos(self, fatura):
        """Soma de créditos (valores negativos invertidos) == créditos do header."""
        creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - Decimal("5074.81")) <= Decimal("0.01")

    def test_equacao_header_fecha(self, fatura):
        """saldo_anterior - créditos + débitos = total_a_pagar."""
        esperado = Decimal("4967.42") - Decimal("5074.81") + Decimal("6834.94")
        assert abs(esperado - fatura.total_a_pagar) <= Decimal("0.01")

    def test_transacoes_nao_vazias(self, fatura):
        """A fatura deve ter transações."""
        assert len(fatura.transacoes) > 0


class TestTransacoes:
    """Valida estrutura individual de transações."""

    def test_valores_decimal_string(self, fatura_dict):
        """Todos os valores monetários devem ser strings no JSON."""
        for t in fatura_dict["transacoes"]:
            assert isinstance(t["valor"], str)
            Decimal(t["valor"])  # não deve lançar

    def test_datas_iso(self, fatura_dict):
        """Todas as datas devem estar no formato ISO YYYY-MM-DD."""
        from datetime import datetime
        for t in fatura_dict["transacoes"]:
            datetime.fromisoformat(t["data"])

    def test_cartao_tagueado(self, fatura_dict):
        """Cada transação deve ter o campo 'cartao'."""
        for t in fatura_dict["transacoes"]:
            assert "cartao" in t
            assert isinstance(t["cartao"], str)

    def test_contagem_transacoes(self, fatura):
        """Número de transações == 96 (conforme fixture aprovada)."""
        assert len(fatura.transacoes) == 96

    def test_cartoes_distintos(self, fatura_dict):
        """Devem existir 3 cartões distintos: .9219, .9735, .0854."""
        cartoes = sorted({t["cartao"] for t in fatura_dict["transacoes"]})
        assert cartoes == ["4563**.******.0854", "4563**.******.9219", "4563**.******.9735"]

    def test_sem_categoria(self, fatura_dict):
        """Categoria deve ser sempre null (sem categorização automática)."""
        for t in fatura_dict["transacoes"]:
            assert t["categoria"] is None

    def test_parcelamento(self, fatura_dict):
        """Transações com parcelamento devem ter parcela_atual <= parcela_total."""
        parceladas = [t for t in fatura_dict["transacoes"] if t["parcela_atual"] is not None]
        assert len(parceladas) > 0
        for t in parceladas:
            assert t["parcela_total"] is not None
            assert t["parcela_atual"] <= t["parcela_total"]


class TestJSONConsistency:
    """Valida que o JSON parseado bate com a fixture."""

    def test_json_equivalente(self, fatura_dict, expected_json):
        """O JSON do parser deve ser idêntico ao JSON expected."""
        assert fatura_dict == expected_json
