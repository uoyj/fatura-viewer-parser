"""Inferência de banco por marcadores (sem PDF)."""
import pytest

from inferencia import inferir_banco


@pytest.mark.parametrize("texto,esperado", [
    ("SOFISA direto Banco\nOlá, JHONATAN...", "sofisa"),
    ("Fatura Nubank\ncomunidade.nu...", "nubank"),
    ("Itaú Unibanco — Fatura do cartão...", "itau"),      # acento normaliza
    ("mercado pago\nOlá, Jhonatan...", "mercadopago"),
    ("Banco qualquer XYZ", None),
    ("", None),
])
def test_inferir_banco(texto, esperado):
    assert inferir_banco(texto, ["sofisa", "nubank", "itau", "mercadopago"]) == esperado


def test_banco_fora_da_lista_nao_retorna():
    # Marcador existe na tabela, mas o banco não está entre os suportados
    assert inferir_banco("SOFISA direto", ["nubank"]) is None
