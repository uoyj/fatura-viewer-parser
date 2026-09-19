"""
Testes para categorizer.py.

Padrão da suíte: o PDF é extraído/parseado UMA vez para o módulo
(`fatura_parseada`, scope="module") — antes cada teste de integração
re-extraía o PDF. Cada teste recebe uma CÓPIA categorizada, para que a
mutação de um teste não vaze para o próximo.

Cobre:
- Unitários: uma descrição representativa por categoria (parametrizado)
- Integração: todas as transações categorizadas + distribuição + somas
  por categoria fechando com o header + idempotência
- Regras: SEED_REGRAS (sem "Outros", com padrões, ordem)

ISOLAMENTO: o fixture autouse redireciona CATEGORIAS_PATH/OVERRIDES_PATH
para tmp_path — nenhum teste toca o data/ real.
"""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

import parsers  # noqa: F401 — side-effect: registra os parsers no registry
from categorizer import SEED_REGRAS, _categorizar_descricao, categorizar
from registry import get_parser

PROJECT_ROOT = Path(__file__).parent.parent
PDF_PATH = PROJECT_ROOT / "in" / "Fatura.pdf"
DEBITOS_ESPERADOS = Decimal("6834.94")
TOL = Decimal("0.01")


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
# Unitários — uma descrição por categoria (matching por substring, case-insensitive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("descricao,categoria", [
    # minúscula de propósito: os demais casos já cobrem o matching em maiúsculas
    ("pagamento de fatura", "Pagamento da fatura"),      # + case-insensitive
    ("Ajuste a Credito ESTAB. PARCEL. DE COMPRA", "Estorno/Ajuste"),
    ("Compra a Vista WELLHUB MARCOS RICIOLI", "Academia/Wellness"),
    ("Compra a Vista RAIA DROGASIL SA", "Farmacia"),
    ("CLINICA VETERINARIA", "Veterinario"),
    ("Compra a Vista IFD*DOM MARTIELLO PIZZ", "Restaurante/Cafe"),
    ("Compra a Vista DG BARBER", "Bar"),
    ("PANIFICADORA E CO", "Supermercado/Mercado"),
    ("Compra a Vista 99FOOD *COLHERADA - MARMI", "Transporte/Apps"),
    ("Compra a Vista IL BARBUTO", "Saude/Podologia"),
    ("AMAZONMKTPLC*LHCOMPROD", "Compras online"),
    ("Compra a Vista DORACIGRINGS", "Outros"),           # fallback implícito
])
def test_categoria(descricao, categoria):
    assert _categorizar_descricao(descricao) == categoria


# ---------------------------------------------------------------------------
# Integração — parser + categorizar (PDF extraído UMA vez por módulo)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fatura_parseada(extract_cached):
    if not PDF_PATH.exists():
        pytest.skip(f"PDF não encontrado em {PDF_PATH}")
    return get_parser("sofisa", "2026-09")(extract_cached(PDF_PATH))


@pytest.fixture
def fatura_categorizada(fatura_parseada):
    """Cópia categorizada — isola a mutação de cada teste."""
    return categorizar(copy.deepcopy(fatura_parseada))


@pytest.mark.slow
def test_integracao_tudo_categorizado_e_distribuicao(fatura_categorizada):
    transacoes = fatura_categorizada.transacoes
    assert len(transacoes) == 96
    for t in transacoes:
        assert t.categoria is not None, f"Transação sem categoria: {t.descricao}"
        assert t.categoria != "", "categoria vazia"

    cats = Counter(t.categoria for t in transacoes)
    assert len(cats) >= 9, f"poucas categorias distintas distribuídas: {dict(cats)}"


@pytest.mark.slow
def test_integracao_somas_por_categoria_e_idempotencia(fatura_categorizada):
    """Soma dos débitos por categoria fecha com o header; recategorizar é estável."""
    soma_por_cat: defaultdict[str, Decimal] = defaultdict(Decimal)
    for t in fatura_categorizada.transacoes:
        if t.valor > 0:
            soma_por_cat[t.categoria] += t.valor

    total = sum(soma_por_cat.values(), Decimal(0))
    assert abs(total - DEBITOS_ESPERADOS) <= TOL, (
        f"Soma de débitos por categoria ({total}) não bate com header "
        f"({DEBITOS_ESPERADOS})"
    )

    antes = [t.categoria for t in fatura_categorizada.transacoes]
    categorizar(fatura_categorizada)  # segunda passada
    depois = [t.categoria for t in fatura_categorizada.transacoes]
    assert antes == depois


# ---------------------------------------------------------------------------
# Regras (seed)
# ---------------------------------------------------------------------------

def test_seed_regras():
    """
    'Outros' é fallback implícito do matching, nunca uma regra; toda regra tem
    padrões (validável por PUT /categorias) e a ordem importa (primeira vence).
    """
    assert all(r["categoria"] != "Outros" for r in SEED_REGRAS)
    assert all(isinstance(r["padroes"], list) and r["padroes"] for r in SEED_REGRAS)
    assert SEED_REGRAS[0]["categoria"] == "Pagamento da fatura"
    assert SEED_REGRAS[-1]["categoria"] == "Compras online"
