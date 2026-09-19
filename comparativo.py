"""
Comparação mês a mês e detecção de gastos anômalos.

Puro: recebe a lista de registros do JSONL (dicts), retorna dict JSON-serializável.
Sem I/O, sem dependência de api.py — testável isoladamente.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import median

# Limiares das regras de anomalia (determinísticas, sem ML)
RAZAO_CATEGORIA = Decimal("1.5")   # mês > 1.5x a mediana dos anteriores
MIN_DIF_CATEGORIA = Decimal("50")  # e diferença absoluta >= R$50
RAZAO_TRANSACAO = Decimal("2.5")   # transação > 2.5x a mediana da descrição
MIN_VALOR_TRANSACAO = Decimal("100")


def _media(vals: list[Decimal]) -> Decimal:
    return sum(vals, Decimal("0")) / Decimal(len(vals))


def calcular_comparativo(registros: list[dict]) -> dict:
    """
    registros: lista de registros do JSONL (cada um com reg["payload"]).
    Retorna: meses, totais por mês, gastos por categoria x mês, anomalias.
    """
    # --- Agregação ---
    por_categoria: dict[str, dict[str, Decimal]] = {}
    totais_mes: dict[str, Decimal] = {}
    por_descricao: dict[str, dict[str, list[Decimal]]] = {}  # desc -> mes -> [valores]

    for reg in registros:
        p = reg.get("payload") or {}
        mes = str(p.get("fechamento", ""))[:7]  # YYYY-MM
        if not mes:
            continue
        totais_mes[mes] = totais_mes.get(mes, Decimal("0"))
        for t in p.get("transacoes") or []:
            try:
                valor = Decimal(str(t["valor"]))
            except Exception:
                continue
            totais_mes[mes] += valor
            cat = t.get("categoria") or "Sem categoria"
            por_categoria.setdefault(cat, {}).setdefault(mes, Decimal("0"))
            por_categoria[cat][mes] += valor
            por_descricao.setdefault((t.get("descricao") or "").strip().upper(), {}) \
                         .setdefault(mes, []).append(valor)

    meses = sorted(totais_mes)

    # --- Anomalias por categoria (mês vs mediana dos anteriores) ---
    anomalias_categoria = []
    for cat, por_mes in por_categoria.items():
        for i, mes in enumerate(meses):
            if mes not in por_mes:
                continue
            anteriores = [por_mes[m] for m in meses[:i] if m in por_mes]
            if len(anteriores) < 1:
                continue
            med = median(anteriores)
            valor = por_mes[mes]
            if med > 0 and valor - med >= MIN_DIF_CATEGORIA and valor > med * RAZAO_CATEGORIA:
                anomalias_categoria.append({
                    "categoria": cat, "mes": mes, "valor": str(valor),
                    "mediana_anterior": str(med),
                    "razao": str((valor / med).quantize(Decimal("0.1"))),
                })

    # --- Anomalias por transação (vs mediana da mesma descrição em outros meses) ---
    anomalias_transacao = []
    for reg in registros:
        p = reg.get("payload") or {}
        mes = str(p.get("fechamento", ""))[:7]
        for t in p.get("transacoes") or []:
            desc = (t.get("descricao") or "").strip().upper()
            try:
                valor = Decimal(str(t["valor"]))
            except Exception:
                continue
            outros = [v for m, vals in por_descricao.get(desc, {}).items()
                      if m != mes for v in vals]
            if len(outros) < 2:
                continue
            med = median(outros)
            if med > 0 and valor >= MIN_VALOR_TRANSACAO and valor > med * RAZAO_TRANSACAO:
                anomalias_transacao.append({
                    "descricao": t.get("descricao"), "data": t.get("data"),
                    "mes": mes, "valor": str(valor), "mediana_descricao": str(med),
                    "razao": str((valor / med).quantize(Decimal("0.1"))),
                    "categoria": t.get("categoria"),
                })

    anomalias_transacao.sort(key=lambda a: Decimal(a["valor"]), reverse=True)

    return {
        "meses": meses,
        "totais_mes": {m: str(v) for m, v in totais_mes.items()},
        "por_categoria": {c: {m: str(v) for m, v in pm.items()}
                          for c, pm in por_categoria.items()},
        "anomalias_categoria": anomalias_categoria,
        "anomalias_transacao": anomalias_transacao,
    }