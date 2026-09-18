"""
Testes para parsers/nubank_2026_09.py.

Valida:
- Header: fechamento, vencimento, total_a_pagar, pagamento_minimo
- Valores esperados: saldo_anterior=194.75, creditos=194.75, debitos=231.26
- Soma global de transações bate com débitos e créditos do header
- Equação: saldo_anterior - creditos + debitos = total_a_pagar
- 11 transações (10 débitos + 1 crédito)
- 2 cartões distintos: "·8188" e "·3528"
- Transação de crédito: "Pagamento em 09 AGO" valor -194.75

Os testes usam skipif para pular silenciosamente quando o PDF
nao existe (ex: CI sem o fixture em in/).
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
PDF_PATH = PROJECT_ROOT / "in" / "Nubank_2026-09-09.pdf"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "nubank_2026_09.expected.json"

# Skip se PDF nao existe
tem_pdf = PDF_PATH.exists()


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestHeader:
    """Valida campos do header da fatura."""

    def test_fechamento(self, fatura):
        assert fatura.fechamento.isoformat() == "2026-09-02"

    def test_vencimento(self, fatura):
        assert fatura.vencimento.isoformat() == "2026-09-09"

    def test_total_a_pagar(self, fatura):
        assert str(fatura.total_a_pagar) == "231.26"

    def test_pagamento_minimo(self, fatura):
        assert str(fatura.pagamento_minimo) == "34.68"


@pytest.mark.skipif(not tem_pdf, reason=f"PDF não encontrado em {PDF_PATH}")
class TestSomas:
    """Valida consistência entre transações e totais do header."""

    def test_soma_debitos(self, fatura):
        """Soma de débitos (valores positivos) == 231.26."""
        debitos = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
        assert abs(debitos - Decimal("231.26")) <= Decimal("0.01")

    def test_soma_creditos(self, fatura):
        """Soma de créditos (valores negativos) == 194.75."""
        creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - Decimal("194.75")) <= Decimal("0.01")

    def test_equacao_header_fecha(self, fatura):
        """saldo_anterior - créditos + débitos = total_a_pagar."""
        esperado = Decimal("194.75") - Decimal("194.75") + Decimal("231.26")
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
        """Número de transações == 11 (10 débitos + 1 crédito)."""
        assert len(fatura.transacoes) == 11

    def test_cartoes_distintos(self, fatura_dict):
        """Devem existir 2 cartões distintos: '·8188' e '·3528'."""
        cartoes = sorted({t["cartao"] for t in fatura_dict["transacoes"] if t["cartao"]})
        assert cartoes == ["·3528", "·8188"]

    def test_sem_categoria(self, fatura_dict):
        """Categoria deve ser sempre null (sem categorização automática)."""
        for t in fatura_dict["transacoes"]:
            assert t["categoria"] is None

    def test_transacao_pagamento(self, fatura):
        """Deve existir uma transação de crédito 'Pagamento em 09 AGO'."""
        pagamentos = [
            t for t in fatura.transacoes if "Pagamento em" in t.descricao
        ]
        assert len(pagamentos) == 1
        p = pagamentos[0]
        assert p.valor == Decimal("-194.75")
        assert p.data.isoformat() == "2026-08-09"

    def test_sem_parcelamento(self, fatura_dict):
        """Nenhuma transação deve ter parcelamento neste layout."""
        for t in fatura_dict["transacoes"]:
            assert t["parcela_atual"] is None
            assert t["parcela_total"] is None


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
    parse_fn = get_parser("nubank", versao="2026-09")
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
