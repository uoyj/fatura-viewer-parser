"""
Parser para faturas Sofisa Direto VISA — layout "2026-09" (REV 3)

Mudanças da REV 3 (simplificação de modelo):
- NÃO há mais dataclass Cartao com seções/flush/merge.
- Output é UMA lista plana de Transacao, cada uma tagueada com "cartao"
  (número mascarado ou "" se desconhecido).
- _extract_cartoes substituído por _extract_transacoes (lista plana).
- _extract_parcelamento: validação de parcelamento invertido (Parc.10/1 → 1/10).
- Validação por SOMA GLOBAL de transações vs. totais do header.
- Header extraído pela equação completa (4 valores num regex único).
- Tudo após "VALOR TOTAL DA FATURA" é ignorado (obrigações futuras).

Principais mudanças da rev 2:
- Transações extraídas por COLUNAS (x-positions), não por linha inteira —
  o layout do Sofisa agrupa N datas + N valores na mesma linha visual.
- ParserError claro quando campo do header é None (nunca TypeError).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from registry import registrar
from schemas import Fatura, Transacao

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

# Token isolado de data: 30/11/25 (sempre DD/MM/YY de 2 dígitos no corpo)
RE_DATA_TOKEN = re.compile(r"^\d{2}/\d{2}/\d{2}$")

# Token isolado de valor: 310,83 | 1.234,56 | -89,90 (âncoras ^$ — token, não substring)
RE_VALOR_TOKEN = re.compile(r"^-?[\d.]+,\d{2}$")

# Parcelamento: "Parc. 4/10", "Parc.10/1", "ParC.6/12"
RE_PARC = re.compile(r"Par[Cc]\.?(\d+)/(\d+)")

# Número mascarado: "4563**.******.9219"
RE_CARTAO = re.compile(r"\d{4}\*\*\.\*+\.\d{4}")

# NOVO — equação completa do resumo (tolerante a quebra de linha entre os termos):
#   R$4.967,42 -R$5.074,81 +R$6.834,94 =R$ 6.727,55
RE_EQUACAO = re.compile(
    r"R\$\s*([\d.]+,\d{2})\s*-\s*R\$\s*([\d.]+,\d{2})\s*\+"
    r"\s*R\$\s*([\d.]+,\d{2})\s*(?:=\s*R\$\s*([\d.]+,\d{2}))?"
)

RE_VALOR_R = re.compile(r"R\$\s*([\d.]+,\d{2})")


class ParserError(Exception):
    """Falha de parse ou validação — mensagem sempre acionável."""


# --------------------------------------------------------------------------
# Helpers básicos
# --------------------------------------------------------------------------

def _parse_data(s: str) -> date | None:
    m = RE_DATA_TOKEN.match(s.strip())
    if not m:
        return None
    d, m_, a = s.strip().split("/")
    return date(2000 + int(a), int(m_), int(d))


def _parse_valor(s: str) -> Decimal | None:
    s = s.strip()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    s = s.replace(".", "").replace(",", ".")
    try:
        v = Decimal(s)
    except Exception:
        return None
    return -v if neg else v


def _extract_parcelamento(descricao: str):
    """
    Extrai parcelamento da descrição.

    Ex: "EC *STERILAIR Parc.10/1 74,80" → ("EC *STERILAIR 74,80", 1, 10)
    O PDF traz "Parc.10/1" = 10 parcelas, atual = 1.
    Se pa > pt, inverte com warning (layout pode ter invertido).
    """
    m = RE_PARC.search(descricao)
    if m:
        pa, pt = int(m.group(1)), int(m.group(2))
        if pa > pt:
            logger.warning(
                "parcelamento invertido '%s' — trocado para %d/%d",
                m.group(0), pt, pa,
            )
            pa, pt = pt, pa
        limpa = RE_PARC.sub("", descricao).strip()
        return limpa, pa, pt
    return descricao, None, None


# --------------------------------------------------------------------------
# Header (página 1)
# --------------------------------------------------------------------------

def _extract_header(page1: dict) -> dict:
    """
    Extrai informações do header da fatura (página 1).

    Layout Sofisa Direto (página 1):
    - Linha "Cheg... até 15/09/2026" → fechamento
    - "Total a pagar" + "R$ 6.727,55"
    - "Vencimento:" + "20/09/2026" (data na linha seguinte)
    - Resumo: "Saldo anterior R$ 4.967,42" | "Créditos - R$ 5.074,81" |
      "Total de débitos + R$ 6.834,94" | "Valor total = R$ 6.727,55"
    """
    lines = page1["lines"]
    texto_pagina = " ".join(l["text"] for l in lines)

    # Fechamento: "até 15/09/2026"
    fechamento = None
    m = re.search(r"até\s*(\d{2})/(\d{2})/(\d{4})", texto_pagina)
    if m:
        fechamento = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # Vencimento: "Vencimento:" + data na mesma linha ou até 2 linhas depois
    vencimento = None
    for i, l in enumerate(lines):
        if "vencimento" in l["text"].lower():
            for j in range(i, min(i + 3, len(lines))):
                m2 = re.search(r"(\d{2})/(\d{2})/(\d{4})", lines[j]["text"])
                if m2:
                    vencimento = date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
                    break
            if vencimento:
                break

    # Resumo pela equação completa (tolerante a quebra de linha)
    saldo_anterior = creditos = debitos = total_a_pagar = None
    m = RE_EQUACAO.search(texto_pagina)
    if m:
        saldo_anterior = _parse_valor(m.group(1))
        creditos = abs(_parse_valor(m.group(2)) or Decimal(0))
        debitos = _parse_valor(m.group(3))
        if m.group(4):
            total_a_pagar = _parse_valor(m.group(4))

    # Fallback primário: extração por contexto individual (valores em lines separadas)
    if saldo_anterior is None:
        for line in lines:
            tl = line["text"].lower()
            vm = RE_VALOR_R.search(line["text"])
            if vm is None:
                continue
            val = _parse_valor(vm.group(1))
            if val is None:
                continue
            if "saldo anterior" in tl and saldo_anterior is None:
                saldo_anterior = val
            if ("créditos" in tl or "creditos" in tl) and creditos is None:
                creditos = abs(val)
            if "total de débitos" in tl and debitos is None:
                debitos = val

    if total_a_pagar is None and None not in (saldo_anterior, creditos, debitos):
        total_a_pagar = saldo_anterior - creditos + debitos
        logger.warning("total_a_pagar computado pela equação (valor explícito ausente no PDF)")

    # Pagamento mínimo: ~10% do total (heurística, sem hardcode)
    pagamento_minimo = None
    if total_a_pagar:
        esperado = total_a_pagar / Decimal("10")
        for l in lines:
            vm = RE_VALOR_R.search(l["text"])
            if vm:
                v = _parse_valor(vm.group(1))
                if v is not None and abs(v - esperado) < Decimal("1.00"):
                    pagamento_minimo = v
                    break
        if pagamento_minimo is None:
            logger.warning("pagamento_minimo não localizado (~10%% do total)")

    return {
        "saldo_anterior": saldo_anterior,
        "creditos": creditos,
        "debitos": debitos,
        "total_a_pagar": total_a_pagar,
        "pagamento_minimo": pagamento_minimo,
        "fechamento": fechamento,
        "vencimento": vencimento,
    }


# --------------------------------------------------------------------------
# Extração de transações por colunas (x-positions) — REV 3
# --------------------------------------------------------------------------

def _parse_linha_multi(words: list[dict]) -> list[Transacao]:
    """
    Recebe as palavras de UMA linha visual e devolve 0..N transações.

    O layout do Sofisa agrupa colunas: uma linha pode conter
    "30/11/25 12/12/25 20/02/26  <descs>  310,83 56,24 119,99"
    = 3 transações. A separação é feita por x-posição:
      - tokens ^DD/MM/YY$          -> coluna de datas
      - tokens ^-?valor$           -> coluna de valores (original; os que
                                      sobram à direita são a coluna "dólar"
                                      e são descartados com log)
      - demais tokens              -> descrição, atribuídos à data cujo x0
                                      é o maior x0 <= x0 do token
    """
    datas, valores, descs = [], [], []
    for w in words:
        t = w["text"]
        if RE_DATA_TOKEN.match(t):
            datas.append(w)
        elif RE_VALOR_TOKEN.match(t):
            valores.append(w)
        else:
            descs.append(w)

    if not datas:
        return []  # linha sem data: continuação/header/lixo — ignora

    datas.sort(key=lambda w: w["x0"])
    valores.sort(key=lambda w: w["x0"])

    if len(valores) < len(datas):
        logger.warning(
            "linha com %d data(s) e só %d valor(es) — ignorada: %r",
            len(datas), len(valores), " ".join(w["text"] for w in words),
        )
        return []

    # Extras à direita = coluna "Valor Equivalente em Dólar" — descarta.
    if len(valores) > len(datas):
        logger.debug("descartados %d token(s) da coluna dólar", len(valores) - len(datas))
        valores = valores[: len(datas)]

    def dono(tok: dict) -> dict:
        cands = [d for d in datas if d["x0"] <= tok["x0"] + 1.0]
        return max(cands, key=lambda d: d["x0"]) if cands else datas[0]

    bucket: dict[int, list[str]] = {id(d): [] for d in datas}
    for tok in sorted(descs, key=lambda w: w["x0"]):
        bucket[id(dono(tok))].append(tok["text"])

    out = []
    for d, v in zip(datas, valores):
        data = _parse_data(d["text"])
        valor = _parse_valor(v["text"])
        if data is None or valor is None:
            continue
        descricao = " ".join(bucket[id(d)]).strip()
        descricao, pa, pt = _extract_parcelamento(descricao)
        if not descricao:
            logger.warning("transação sem descrição: %s %s", d["text"], v["text"])
        out.append(Transacao(
            data=data, descricao=descricao, valor=valor,
            parcela_atual=pa, parcela_total=pt,
        ))
    return out


# --------------------------------------------------------------------------
# NOVO — lista plana de transações (REV 3: sem Cartao)
# --------------------------------------------------------------------------

def _extract_transacoes(pages: list[dict]) -> list[Transacao]:
    """
    Lista plana de transações, cada uma tagueada com o último número
    mascarado visto (coluna "cartao"). Não há objetos Cartao.

    Layout real: "Titular / 4563**.******.XXXX / Data Descricao... / transações"
    — o número SEMPRE precede as transações dele, então basta atualizar a
    tag quando o número aparece. Linhas de titular e headers são ignoradas
    naturalmente (não têm token de data, _parse_linha_multi devolve []).
    Tudo após "VALOR TOTAL DA FATURA" / "Saldo total consolidado" é ignorado.
    """
    transacoes: list[Transacao] = []
    cartao_atual = ""
    fim = False
    for page in pages[1:]:  # pág. 1 = header, sem transações
        if fim:
            break
        for line in page["lines"]:
            text = line["text"].strip()
            if not text:
                continue
            low = text.lower()
            if "valor total da fatura" in low or "saldo total consolidado" in low:
                fim = True
                break
            m = RE_CARTAO.search(text)
            if m:
                cartao_atual = m.group(0)
                continue
            for t in _parse_linha_multi(line["words"]):
                t.cartao = cartao_atual
                transacoes.append(t)
    return transacoes


# --------------------------------------------------------------------------
# Validação com soma global e checagem de None
# --------------------------------------------------------------------------

TOLERANCIA = Decimal("0.01")


def _validate(fatura: Fatura, header: dict) -> None:
    # 1. Nenhum campo do header pode faltar
    for campo in ("saldo_anterior", "creditos", "debitos",
                  "total_a_pagar", "pagamento_minimo",
                  "fechamento", "vencimento"):
        if header.get(campo) is None:
            raise ParserError(f"campo '{campo}' não encontrado no header da fatura")

    saldo_anterior = header["saldo_anterior"]
    creditos = header["creditos"]
    debitos = header["debitos"]

    # 2. Equação do header fecha: anterior - créditos + débitos = total
    esperado = saldo_anterior - creditos + debitos
    if abs(esperado - fatura.total_a_pagar) > TOLERANCIA:
        raise ParserError(
            f"equação do header não fecha: {saldo_anterior} - {creditos} + {debitos} "
            f"= {esperado}, mas total_a_pagar = {fatura.total_a_pagar}"
        )

    # 3. SOMA GLOBAL das transações bate com os totais do header
    todas = fatura.transacoes
    soma_debitos = sum((t.valor for t in todas if t.valor > 0), Decimal(0))
    soma_creditos = -sum((t.valor for t in todas if t.valor < 0), Decimal(0))

    if abs(soma_debitos - debitos) > TOLERANCIA:
        raise ParserError(
            f"soma de débitos extraída ({soma_debitos}) != total de débitos do "
            f"header ({debitos}) — diff {abs(soma_debitos - debitos)}"
        )
    if abs(soma_creditos - creditos) > TOLERANCIA:
        raise ParserError(
            f"soma de créditos extraída ({soma_creditos}) != créditos do header "
            f"({creditos}) — diff {abs(soma_creditos - creditos)}"
        )


# --------------------------------------------------------------------------
# Parser principal
# --------------------------------------------------------------------------

@registrar("sofisa", "2026-09")
def parse_sofisa_2026_09(extractor_output: dict) -> Fatura:
    """
    Parser para faturas Sofisa Direto VISA (layout 2026-09).

    Args:
        extractor_output: output de extract_text() — dict com chave "pages".

    Returns:
        Fatura — conforme schemas.py.
    """
    pages = extractor_output.get("pages", [])
    if not pages:
        raise ParserError("PDF vazio ou sem páginas")

    header = _extract_header(pages[0])
    transacoes = _extract_transacoes(pages)

    fatura = Fatura(
        banco="sofisa",
        modelo="2026-09",
        fechamento=header["fechamento"],
        vencimento=header["vencimento"],
        total_a_pagar=header["total_a_pagar"],
        pagamento_minimo=header["pagamento_minimo"],
        transacoes=transacoes,
    )
    _validate(fatura, header)
    return fatura
