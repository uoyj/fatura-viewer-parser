"""Testes de calcular_consolidado — dados sintéticos, sem PDF."""
from comparativo import calcular_consolidado


def _reg(fechamento, transacoes):
    return {"id": "x", "payload": {
        "fechamento": fechamento, "vencimento": fechamento,
        "banco": "b", "modelo": "m", "total_a_pagar": "0",
        "pagamento_minimo": "0", "transacoes": transacoes,
    }}


def _tx(desc, valor, pa=None, pt=None, cat="Outros"):
    return {"data": "2026-09-01", "descricao": desc, "valor": str(valor),
            "parcela_atual": pa, "parcela_total": pt, "categoria": cat,
            "cartao": "Visa ••1234"}


REGISTROS = [
    _reg("2026-09-09", [
        _tx("Streaming FL", 50, cat="Assinaturas"),          # recorrente
        _tx("Compra X", 100, pa=2, pt=4, cat="Compras"),     # 2 parcelas restantes
        _tx("Estorno Y", -20),
    ]),
    _reg("2026-08-15", [
        _tx("Compra Z", 30, pa=1, pt=3, cat="Compras"),      # 2 parcelas restantes
    ]),
]


def test_projecao_agrupa_todas_faturas():
    c = calcular_consolidado(REGISTROS, {"STREAMING FL"})

    proj = c["projecao"]
    # Fatura A (fech 2026-09): parcela 100 em 10 e 11; recorrente 50 por 12 meses
    # Fatura B (fech 2026-08): parcela 30 em 09 e 10
    assert proj["2026-09"]["total"] == "30.00"
    assert proj["2026-10"]["total"] == "180.00"   # 100 + 50 + 30
    assert proj["2026-11"]["total"] == "150.00"   # 100 + 50
    assert proj["2027-09"]["total"] == "50.00"    # recorrente mês 12

    # Estorno (valor negativo) nunca entra na projeção
    linhas_10 = [l["descricao"] for l in proj["2026-10"]["linhas"]]
    assert "Estorno Y" not in linhas_10
    # Info marca recorrente
    rec = [l for l in proj["2026-10"]["linhas"] if l["info"] == "Recorrente"]
    assert len(rec) == 1 and rec[0]["valor"] == "50"

    # Histórico vem do comparativo intacto
    assert "2026-09" in c["totais_mes"]


def test_sem_parcelas_nem_recorrentes_projecao_vazia():
    c = calcular_consolidado([_reg("2026-09-09", [_tx("À vista", 10)])], set())
    assert c["projecao"] == {}
