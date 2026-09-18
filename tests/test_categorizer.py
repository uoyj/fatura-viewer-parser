"""
Testes para categorizer.py.

Cobre:
- Unitários: uma transação por categoria (case-insensitive substring matching)
- Integração: parser sofisa_2026_09 em Fatura.pdf → categorizar → todas as
  transações têm categoria != null, e somas por categoria fecham com totais
"""

from datetime import date
from decimal import Decimal

import pytest
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import Transacao, Fatura
from categorizer import categorizar, _categorizar_descricao, SEED_REGRAS


@pytest.fixture(autouse=True)
def _isolar_categorias(tmp_path, monkeypatch):
    """
    Nenhum teste toca o data/ real: as regras vêm do seed gravado em tmp_path.

    As regras/overrides agora vivem em JSON editável (data/categorias.json,
    data/overrides.json), então a fonte precisa ser redirecionada aqui.
    """
    import categorizer
    monkeypatch.setattr(categorizer, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(categorizer, "OVERRIDES_PATH", tmp_path / "overrides.json")


# ---------------------------------------------------------------------------
# Unitários — uma descrição por categoria
# ---------------------------------------------------------------------------

class TestCategoriasUnitarias:
    """Testa cada categoria com uma descrição representativa real do PDF."""

    def test_pagamento_fatura(self):
        assert _categorizar_descricao("Pagamento de Fatura") == "Pagamento da fatura"

    def test_estorno_ajuste(self):
        assert _categorizar_descricao("Ajuste a Credito") == "Estorno/Ajuste"

    def test_estorno_ajuste_com_estab(self):
        assert _categorizar_descricao("Ajuste a Credito ESTAB. PARCEL. DE COMPRA") == "Estorno/Ajuste"

    def test_academia_wellhub(self):
        assert _categorizar_descricao("Compra a Vista WELLHUB MARCOS RICIOLI") == "Academia/Wellness"

    def test_farmacia_raia_drogasil(self):
        assert _categorizar_descricao("Compra a Vista RAIA DROGASIL SA") == "Farmacia"

    def test_farmacia_panvel(self):
        assert _categorizar_descricao("PANVEL FILIAL 500") == "Farmacia"

    def test_veterinario(self):
        assert _categorizar_descricao("CLINICA VETERINARIA") == "Veterinario"

    def test_restaurante_cafe(self):
        assert _categorizar_descricao("Compra a Vista NONO CAFE") == "Restaurante/Cafe"

    def test_restaurante_everest_inn(self):
        assert _categorizar_descricao("Compra a Vista EVEREST INN") == "Restaurante/Cafe"

    def test_restaurante_dom_martiello(self):
        assert _categorizar_descricao("Compra a Vista IFD*DOM MARTIELLO PIZZ") == "Restaurante/Cafe"

    def test_bar_dg_barber(self):
        assert _categorizar_descricao("Compra a Vista DG BARBER") == "Bar"

    def test_bar_deep_lounge(self):
        assert _categorizar_descricao("Compra a Vista DEEP LOUNGE BAR") == "Bar"

    def test_supermercado_mercadolivre(self):
        assert _categorizar_descricao("MERCADO*MERCADOLIVRE*") == "Supermercado/Mercado"

    def test_supermercado_panificadora(self):
        assert _categorizar_descricao("PANIFICADORA E CO") == "Supermercado/Mercado"

    def test_supermercado_casa_carnes(self):
        assert _categorizar_descricao("Compra a Vista CASA*CARNES*TIROLEZ") == "Supermercado/Mercado"

    def test_transporte_99food(self):
        assert _categorizar_descricao("Compra a Vista 99FOOD *COLHERADA - MARMI") == "Transporte/Apps"

    def test_transporte_99(self):
        assert _categorizar_descricao("Compra a Vista 99*") == "Transporte/Apps"

    def test_saude_podomax(self):
        assert _categorizar_descricao("Compra a Vista PODOMAX") == "Saude/Podologia"

    def test_saude_il_barbudo(self):
        assert _categorizar_descricao("Compra a Vista IL BARBUTO") == "Saude/Podologia"

    def test_compras_online_amazon(self):
        assert _categorizar_descricao("EC *STERILAIR") == "Compras online"

    def test_compras_online_shopee(self):
        assert _categorizar_descricao("SHOPEE *MUNDOTECHIMPOR") == "Compras online"

    def test_compras_online_shein(self):
        assert _categorizar_descricao("Compra a Vista BRS*SHEINCOM") == "Compras online"

    def test_compras_online_amazonmktplc(self):
        assert _categorizar_descricao("AMAZONMKTPLC*LHCOMPROD") == "Compras online"

    def test_compras_online_alargadores(self):
        assert _categorizar_descricao("ALARGADORES.*ALAR") == "Compras online"

    def test_outros_doracirings(self):
        assert _categorizar_descricao("Compra a Vista DORACIGRINGS") == "Outros"

    def test_outros_chq(self):
        assert _categorizar_descricao("Compra a Vista CHQ CURITIBA 06") == "Outros"

    def test_outros_xv_novembro(self):
        assert _categorizar_descricao("XV DE NOVEMBRO") == "Outros"

    def test_case_insensitive(self):
        """Matching é case-insensitive."""
        assert _categorizar_descricao("pagamento de fatura") == "Pagamento da fatura"
        assert _categorizar_descricao("Pagamento De Fatura") == "Pagamento da fatura"


# ---------------------------------------------------------------------------
# Integração — parser + categorizar
# ---------------------------------------------------------------------------

@pytest.fixture
def fatura_parseada():
    from extractors.pdf_extractor import extract_text
    from registry import get_parser
    import parsers  # noqa: F401

    output = extract_text(Path("in/Fatura.pdf"))
    parse_fn = get_parser("sofisa", "2026-09")
    fatura = parse_fn(output)
    return fatura


class TestCategorizacaoIntegrada:

    def test_todas_transacoes_categorizadas(self, fatura_parseada):
        fatura = categorizar(fatura_parseada)
        assert len(fatura.transacoes) == 96
        for t in fatura.transacoes:
            assert t.categoria is not None, f"Transação sem categoria: {t.descricao}"
            assert t.categoria != "", "categoria vazia"

    def test_distribuicao_categorias(self, fatura_parseada):
        fatura = categorizar(fatura_parseada)
        from collections import Counter
        cats = Counter(t.categoria for t in fatura.transacoes)
        print(f"\nDistribuição: {dict(cats)}")
        assert len(cats) >= 9  # pelo menos 9 categorias diferentes

    def test_somas_por_categoria(self, fatura_parseada):
        """Soma dos débitos (valores positivos) por categoria fecha com total."""
        fatura = categorizar(fatura_parseada)
        from collections import defaultdict
        soma_por_cat = defaultdict(Decimal)
        for t in fatura.transacoes:
            if t.valor > 0:
                soma_por_cat[t.categoria] += t.valor
        total = sum(soma_por_cat.values())
        assert abs(total - Decimal("6834.94")) <= Decimal("0.01"), \
            f"Soma de débitos por categoria ({total}) não bate com header (6834.94)"

    def test_categorizacao_idempotente(self, fatura_parseada):
        """Rodar categorizar duas vezes dá o mesmo resultado."""
        fatura = categorizar(fatura_parseada)
        cats1 = [t.categoria for t in fatura.transacoes]
        fatura = categorizar(fatura)
        cats2 = [t.categoria for t in fatura.transacoes]
        assert cats1 == cats2


# ---------------------------------------------------------------------------
# Testes de regras
# ---------------------------------------------------------------------------

class TestRegras:
    """
    O dicionário de regras agora vive em data/categorias.json (editável).

    SEED_REGRAS é apenas o valor inicial gravado quando o arquivo não existe —
    e "Outros" é o fallback implícito do matching, nunca uma regra.
    """

    def test_seed_sem_outros_como_regra(self):
        assert all(r["categoria"] != "Outros" for r in SEED_REGRAS)

    def test_seed_ordenado(self):
        """Regras específicas vêm primeiro (ordem importa: primeira que casa vence)."""
        assert SEED_REGRAS[0]["categoria"] == "Pagamento da fatura"
        assert SEED_REGRAS[-1]["categoria"] == "Compras online"

    def test_seed_com_padroes(self):
        """Toda regra do seed precisa ter pelo menos 1 padrão (validável por PUT /categorias)."""
        for regra in SEED_REGRAS:
            assert isinstance(regra["padroes"], list) and regra["padroes"]
