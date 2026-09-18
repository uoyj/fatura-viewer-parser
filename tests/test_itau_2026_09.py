"""
Testes para parsers/itau_2026_09.py.

Cobre DUAS faturas do MESMO modelo (parametrizadas):
- Fatura_Itau_20260917-204150.pdf (Gabriel, fechamento 03/09/2026)
  → fixture itau_2026_09.expected.json
- Fatura_Itau_20260918-151358.pdf (Wesley, fechamento 08/09/2026)
  → fixture itau_2026_09_b.expected.json

Valida:
- Header: fechamento, vencimento, total_a_pagar, pagamento_minimo, cartão
- ENCARGOS: campo próprio do header (default 0) e equação
  saldo_anterior - creditos + debitos + encargos = total
- Soma global das transações == débitos (compras); encargos NÃO entram na soma
  (ficam fora da tabela de lançamentos)
- PARCELA NN/MM colada no estabelecimento ("nuuvem *N 02/06" = parcela 2 de 6)
- Limpeza do sufixo "categoria CIDADE" na descrição
- Estrutura: datas ISO, valores string, cartão tagueado
- Consistência do JSON com o fixture (igualdade exata)

Os testes usam parametrize + skipif para pular silenciosamente quando o PDF
não existe (ex: CI sem os fixtures em in/).

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

# --- Caminhos ---
PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR = PROJECT_ROOT / "in"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

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


# ---------------------------------------------------------------------------
# Fixtures (module scope — extrai/parseia uma vez por fatura)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", params=list(FACTS), ids=list(FACTS))
def fact(request):
    return FACTS[request.param]


@pytest.fixture(scope="module")
def pdf_path(fact):
    return PDF_DIR / fact["pdf"]


@pytest.fixture(scope="module")
def extractor_output(pdf_path):
    if not pdf_path.exists():
        pytest.skip(f"PDF não encontrado em {pdf_path}")
    return extract_text(pdf_path)


@pytest.fixture(scope="module")
def header(extractor_output):
    return _extract_header(extractor_output["pages"])


@pytest.fixture(scope="module")
def fatura(extractor_output):
    return get_parser("itau", "2026-09")(extractor_output)


@pytest.fixture(scope="module")
def fatura_dict(fatura):
    return fatura_para_dict(fatura)


@pytest.fixture(scope="module")
def expected_json(fact):
    with open(FIXTURES_DIR / fact["fixture"]) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

class TestHeader:

    def test_fechamento(self, fatura, fact):
        assert fatura.fechamento.isoformat() == fact["fechamento"]

    def test_vencimento(self, fatura, fact):
        assert fatura.vencimento.isoformat() == fact["vencimento"]

    def test_total_a_pagar(self, fatura, fact):
        assert str(fatura.total_a_pagar) == fact["total_a_pagar"]

    def test_pagamento_minimo(self, fatura, fact):
        assert str(fatura.pagamento_minimo) == fact["pagamento_minimo"]

    def test_saldo_anterior_e_creditos(self, header, fact):
        assert header["saldo_anterior"] == fact["saldo_anterior"]
        assert header["creditos"] == fact["creditos"]


# ---------------------------------------------------------------------------
# Encargos (campo novo do header)
# ---------------------------------------------------------------------------

class TestEncargos:

    def test_encargos_no_header(self, header, fact):
        assert header["encargos"] == fact["encargos"]

    def test_debitos_do_header(self, header, fact):
        assert header["debitos"] == fact["debitos"]

    def test_equacao_do_header_fecha_com_encargos(self, header, fatura, fact):
        """saldo_anterior - creditos + debitos + encargos = total_a_pagar."""
        esperado = (
            header["saldo_anterior"] - header["creditos"]
            + header["debitos"] + header["encargos"]
        )
        assert abs(esperado - fatura.total_a_pagar) <= Decimal("0.01")
        assert esperado == Decimal(fact["total_a_pagar"])

    def test_encargos_ficam_fora_da_soma_das_transacoes(self, fatura, fact):
        """A soma das transações bate com os débitos; encargos não entram nela."""
        soma = sum((t.valor for t in fatura.transacoes if t.valor > 0), Decimal(0))
        assert abs(soma - fact["debitos"]) <= Decimal("0.01")
        if fact["encargos"]:
            assert soma != fact["debitos"] + fact["encargos"]


# ---------------------------------------------------------------------------
# Somas e estrutura
# ---------------------------------------------------------------------------

class TestSomas:

    def test_soma_creditos(self, fatura, fact):
        creditos = -sum((t.valor for t in fatura.transacoes if t.valor < 0), Decimal(0))
        assert abs(creditos - fact["creditos"]) <= Decimal("0.01")

    def test_transacoes_nao_vazias(self, fatura):
        assert len(fatura.transacoes) > 0


class TestTransacoes:

    def test_valores_decimal_string(self, fatura_dict):
        for t in fatura_dict["transacoes"]:
            assert isinstance(t["valor"], str)
            Decimal(t["valor"])  # não deve lançar

    def test_datas_iso(self, fatura_dict):
        for t in fatura_dict["transacoes"]:
            datetime.fromisoformat(t["data"])

    def test_cartao_tagueado(self, fatura_dict, fact):
        cartoes = {t["cartao"] for t in fatura_dict["transacoes"]}
        assert cartoes == {fact["cartao"]}

    def test_ids_sequenciais(self, fatura_dict):
        ids = [t["id"] for t in fatura_dict["transacoes"]]
        assert ids == list(range(len(ids)))


# ---------------------------------------------------------------------------
# Parcela NN/MM embutida no estabelecimento
# ---------------------------------------------------------------------------

class TestParcelasNaDescricao:

    def test_parcelas_esperadas(self, fatura, fact):
        mapa = {
            t.descricao: (t.parcela_atual, t.parcela_total)
            for t in fatura.transacoes if t.parcela_atual is not None
        }
        for descricao, parcela in fact["parcelas"].items():
            assert descricao in mapa, f"{descricao!r} ausente (tem: {sorted(mapa)})"
            assert mapa[descricao] == parcela

    def test_nenhuma_descricao_termina_com_token_ddmm(self, fatura):
        """O token de parcela sai da descrição."""
        for t in fatura.transacoes:
            assert not re.search(r"\d{2}/\d{2}", t.descricao), t.descricao

    def test_parcela_atual_menor_ou_igual_total(self, fatura):
        for t in fatura.transacoes:
            if t.parcela_atual is not None:
                assert t.parcela_total is not None
                assert t.parcela_atual <= t.parcela_total


class TestHelpersDeDescricao:
    """Funções puras usadas na montagem da descrição."""

    @pytest.mark.parametrize("texto,esperado", [
        ("nuuvem *N 02/06", (2, 6)),
        ("FARMACIA DRO*N06/06", (6, 6)),
        ("JIM.COM* IBUY03/12", (3, 12)),
        ("FILIAL523CTB02/03", (2, 3)),
        ("AMAZONMKTPLC*M 03/10", (3, 10)),
        ("LOJA SEM PARCELA", (None, None)),
        ("PAGTO 13/08 LOJA", (None, None)),   # token com cara de DATA não é parcela
    ])
    def test_extrair_parcela(self, texto, esperado):
        assert _extrair_parcela(texto) == esperado

    def test_remove_sufixo_categoria_cidade(self):
        assert _limpar_categoria_cidade(
            "LANCHONETE DOIS CORACCU supermercado CURITIBA") == "LANCHONETE DOIS CORACCU"

    def test_nao_remove_quando_o_estabelecimento_comeca_com_a_categoria(self):
        assert _limpar_categoria_cidade("LANCHONETE DOIS CORACCU") == "LANCHONETE DOIS CORACCU"

    def test_remove_ate_dois_sufixos(self):
        assert _limpar_categoria_cidade(
            "LOJA X transporte SAO PAULO supermercado CURITIBA") == "LOJA X"

    def test_descricao_sem_sufixo_fica_intacta(self):
        for descricao in ("AMAZON BR", "FARMACIA DRO*N", "Compra a Vista NONO CAFE"):
            assert _limpar_categoria_cidade(descricao) == descricao


# ---------------------------------------------------------------------------
# Consistência do JSON
# ---------------------------------------------------------------------------

class TestJSONConsistency:

    def test_json_equivalente(self, fatura_dict, expected_json):
        assert fatura_dict == expected_json
