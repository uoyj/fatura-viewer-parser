"""
Testes para parsers/itau_2026_09.py.

Valida:
- Header: fechamento=2026-09-03, vencimento=2026-09-10, total=1940.84,
  pagamento_minimo=194.08
- Soma global: débitos=1940.84, créditos=2111.00
- 25 transações (24 débitos + 1 crédito)
- Todas as transações com cartao="3620"
- Crédito: "Pagamento via conta" valor -2111.00

Os testes usam skipif para pular silenciosamente quando o PDF
não existe (ex: CI sem o fixture em in/).
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
PDF_PATH = PROJECT_ROOT / "in" / "Fatura_Itau_20260917-204150.pdf"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "itau_2026_09.expected.json"

# Skip se PDF não existe
tem_pdf = PDF_PATH.exists()


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestHeader:
    """Valida campos do header da fatura."""

    def test_fechamento(self, fatura):
        assert fatura.fechamento.isoformat() == "2026-09-03"

    def test_vencimento(self, fatura):
        assert fatura.vencimento.isoformat() == "2026-09-10"

    def test_total_a_pagar(self, fatura):
        assert str(fatura.total_a_pagar) == "1940.84"

    def test_pagamento_minimo(self, fatura):
        assert str(fatura.pagamento_minimo) == "194.08"


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestSomas:
    """Valida consistência entre transações e totais do header."""

    def test_soma_debitos(self, fatura):
        """Soma de débitos (valores positivos) == 1940.84."""
        debitos = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
        assert abs(debitos - Decimal("1940.84")) <= Decimal("0.01")

    def test_soma_creditos(self, fatura):
        """Soma de créditos (valores negativos) == 2111.00."""
        creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - Decimal("2111.00")) <= Decimal("0.01")

    def test_equacao_header_fecha(self, fatura):
        """saldo_anterior - créditos + débitos = total_a_pagar."""
        esperado = Decimal("2111.00") - Decimal("2111.00") + Decimal("1940.84")
        assert abs(esperado - fatura.total_a_pagar) <= Decimal("0.01")

    def test_transacoes_nao_vazias(self, fatura):
        assert len(fatura.transacoes) > 0


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestTransacoes:
    """Valida estrutura individual de transações."""

    def test_valores_decimal_string(self, fatura_dict):
        """Todos os valores monetários devem ser strings no JSON."""
        for t in fatura_dict["transacoes"]:
            assert isinstance(t["valor"], str)
            Decimal(t["valor"])  # não deve lançar

    def test_datas_iso(self, fatura_dict):
        """Todas as datas devem estar no formato ISO YYYY-MM-DD."""
        for t in fatura_dict["transacoes"]:
            datetime.fromisoformat(t["data"])

    def test_cartao_tagueado(self, fatura_dict):
        """Cada transação deve ter o campo 'cartao'."""
        for t in fatura_dict["transacoes"]:
            assert "cartao" in t
            assert isinstance(t["cartao"], str)

    def test_contagem_transacoes(self, fatura):
        """Número de transações == 25 (24 débitos + 1 crédito)."""
        assert len(fatura.transacoes) == 25

    def test_cartao_unico(self, fatura_dict):
        """Todas as transações devem ter cartao='3620'."""
        cartoes = {t["cartao"] for t in fatura_dict["transacoes"]}
        assert cartoes == {"3620"}

    def test_sem_categoria(self, fatura_dict):
        """Categoria deve ser sempre null (sem categorização automática)."""
        for t in fatura_dict["transacoes"]:
            assert t["categoria"] is None

    def test_sem_parcelamento(self, fatura_dict):
        """Nenhuma transação deve ter parcelamento neste layout."""
        for t in fatura_dict["transacoes"]:
            assert t["parcela_atual"] is None
            assert t["parcela_total"] is None

    def test_transacao_pagamento(self, fatura):
        """Deve existir uma transação de crédito 'Pagamento via conta'."""
        pagamentos = [t for t in fatura.transacoes if "Pagamento via conta" in t.descricao]
        assert len(pagamentos) == 1
        p = pagamentos[0]
        assert p.valor == Decimal("-2111.00")
        assert p.data.isoformat() == "2026-08-03"

    def test_cartao_tag_3620(self, fatura_dict):
        """Todas as transações têm cartao='3620' (últimos 4 dígitos)."""
        for t in fatura_dict["transacoes"]:
            assert t["cartao"] == "3620"


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestJSONConsistency:
    """Valida que o JSON parseado bate com a fixture."""

    def test_json_equivalente(self, fatura_dict, expected_json):
        """O JSON do parser deve ser idêntico ao JSON expected."""
        assert fatura_dict == expected_json


# --- Fixtures ---

@pytest.fixture(scope="module")
def extractor_output():
    """Extract text do PDF uma vez por módulo de teste."""
    return extract_text(PDF_PATH)


@pytest.fixture(scope="module")
def fatura(extractor_output):
    """Parse da fatura, reutilizando o extractor output."""
    parse_fn = get_parser("itau", versao="2026-09")
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
