"""
Comparação mês a mês e detecção de gastos anômalos.

Puro: recebe a lista de registros do JSONL (dicts), retorna dict JSON-serializável.
Sem I/O, sem dependência de api.py — testável isoladamente.

Semântica: "total do mês" = SOMA DE DÉBITOS (valor > 0). Pagamentos de fatura e
estornos (valores negativos) NÃO entram — não representam gasto do mês.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import median

# Limiares das regras de anomalia (determinísticas, sem ML)
RAZAO_CATEGORIA = Decimal("1.5")   # mês > 1.5x a mediana dos anteriores
MIN_DIF_CATEGORIA = Decimal("50")  # e diferença absoluta >= R$50
RAZAO_TRANSACAO = Decimal("2.5")   # transação > 2.5x a mediana da descrição
MIN_VALOR_TRANSACAO = Decimal("100")

CAP_MESES = 12  # mesmo teto do projetarParcelas() do frontend


def _add_months(y: int, m: int, k: int) -> tuple[int, int]:
    """(ano, mês) + k meses, com virada de ano."""
    total = y * 12 + (m - 1) + k
    return total // 12, total % 12 + 1


def calcular_comparativo(registros: list[dict]) -> dict:
    """
    registros: lista de registros do JSONL (cada um com reg["payload"]).
    Retorna: meses, totais por mês (só débitos), gastos por categoria x mês,
    anomalias.
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
            if valor <= 0:   # gasto = débitos; pagamentos/estornos fora
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
            if valor <= 0:
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


def calcular_consolidado(registros: list[dict], recorrentes: set[str]) -> dict:
    """
    Visão consolidada: reusa a agregação histórica do calcular_comparativo
    e adiciona a projeção futura agregando TODAS as faturas.

    recorrentes: set de descrições normalizadas (mesma chave do
    data/recorrentes.json — upper + espaços colapsados).

    Retorna tudo do comparativo + "projecao":
      {"2026-10": {"total": "180.00",
                   "linhas": [{"descricao", "cartao", "valor", "info"}]},
       ...}
    info = "N/M" (parcela projetada) ou "Recorrente".
    """
    base = calcular_comparativo(registros)
    proj: dict[str, dict] = {}

    for reg in registros:
        p = reg.get("payload") or {}
        fech = str(p.get("fechamento") or "")
        if not fech:
            continue
        try:
            y, m, _ = fech.split("-")
            y, m = int(y), int(m)
        except ValueError:
            continue

        for t in p.get("transacoes") or []:
            try:
                valor = Decimal(str(t["valor"]))
            except Exception:
                continue
            if valor <= 0:
                continue

            desc = " ".join(str(t.get("descricao") or "").split()).upper()
            pa, pt = t.get("parcela_atual"), t.get("parcela_total")

            if desc in recorrentes:
                meses = range(1, CAP_MESES + 1)
                info_fn = lambda k: "Recorrente"
            elif isinstance(pa, int) and isinstance(pt, int) and 0 < pa < pt:
                meses = range(1, min(pt - pa, CAP_MESES) + 1)
                info_fn = lambda k: f"{pa + k}/{pt}"
            else:
                continue

            for k in meses:
                ny, nm = _add_months(y, m, k)
                key = f"{ny:04d}-{nm:02d}"
                bloco = proj.setdefault(key, {"total": Decimal("0"), "linhas": []})
                bloco["total"] += valor
                bloco["linhas"].append({
                    "descricao": t.get("descricao") or "",
                    "cartao": t.get("cartao") or "",
                    "valor": str(valor),
                    "info": info_fn(k),
                })

    for bloco in proj.values():
        bloco["linhas"].sort(key=lambda l: Decimal(l["valor"]), reverse=True)

    return {
        "meses": base["meses"],
        "totais_mes": base["totais_mes"],
        "por_categoria": base["por_categoria"],
        "projecao": {m: {"total": str(b["total"]), "linhas": b["linhas"]}
                     for m, b in sorted(proj.items())},
    }