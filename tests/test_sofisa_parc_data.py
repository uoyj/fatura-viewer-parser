"""Parcela calculada pela data da compra — parser Sofisa (sem PDF)."""
from datetime import date

import pytest

from parsers.sofisa_2026_09 import _parcela_atual_por_data, _extract_parcelamento


@pytest.mark.parametrize("compra,fech,esperado", [
    (date(2025, 11, 30), date(2026, 9, 15), 10),  # STERILAIR: real 10/10
    (date(2026, 2, 20),  date(2026, 9, 15), 7),   # compra >15 → 1ª em mar
    (date(2026, 8, 16),  date(2026, 9, 15), 1),   # XV DE NOVEMBRO
    (date(2026, 8, 15),  date(2026, 9, 15), 2),   # até dia 15 → 1ª no próprio mês
])
def test_parcela_atual_por_data(compra, fech, esperado):
    assert _parcela_atual_por_data(compra, fech) == esperado


def test_parc_truncado_usa_data_da_compra():
    desc, pa, pt = _extract_parcelamento(
        "EC *STERILAIR Parc.10/1 74,80", date(2025, 11, 30), date(2026, 9, 15))
    assert (pa, pt) == (10, 10)
    assert "Parc" not in desc


def test_parc_intacto_confia_na_string():
    desc, pa, pt = _extract_parcelamento(
        "SHOPEE *X Parc.4/10", date(2026, 5, 19), date(2026, 9, 15))
    assert (pa, pt) == (4, 10)


def test_sem_data_mantem_fallback_inversao():
    _, pa, pt = _extract_parcelamento("X Parc.10/1")
    assert (pa, pt) == (1, 10)
