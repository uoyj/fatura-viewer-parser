"""
Parser para faturas Nubank — layout "2026-09".

Arquitetura seguida da mesma do sofisa_2026_09:
- Parser recebe o output do extractor (pages), nunca lê PDF diretamente.
- Header extraído de múltiplas páginas (page 1 para vencimento/fechamento,
  page 4/RESUMO para saldo_anterior/creditos/debitos/total_a_pagar).
- Transações extraídas da página 5 (seção "TRANSAÇÕES DE 02 AGO A 02 SET").
- Validação por equação do header + soma global das transações.

Layout do Nubank (fatura 09SET2026):
- Page 1: header básico — "Data de vencimento: 09 SET 2026", "Período vigente: 02 AGO a 02 SET"
- Page 4: "RESUMO DA FATURA ATUAL" — "Fatura anterior R$ 194,75",
  "Pagamento recebido −R$ 194,75" (U+2212), "Total a pagar R$ 231,26",
  "Pagamento mínimo... R$ 34,68"
- Page 5: "TRANSAÇÕES DE 02 AGO A 02 SET" — tabela de compras seguida de
  "Pagamentos e Financiamentos".

Diferenças do layout vs sofisa:
- Cartões identificados por "•••• 8188" / "•••• 3528" (não formato mascarado 4563**)
- Valores monetários no formato "R$ 7,18" (com espaço após R$)
- Sinal negativo usa U+2212 (−), não hífen ASCII — normalizar antes de parsear
- Data das transações: "12 AGO" (DD + MMM abreviado em maiúsculas)
- Layout: cartão+descricao+valor em uma linha, data na linha seguinte (mesmo top)
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

# Mês abreviado em português (maiúsculas, como no PDF do Nubank)
MESES = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

# Data no formato "DD MMM" (ex: "12 AGO")
RE_DATA_DDMMM = re.compile(r"^(\d{2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)$")

# Data de vencimento no header: "09 SET 2026"
RE_DATA_VENCIMENTO = re.compile(r"Data de vencimento:\s*(\d{2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\s+(\d{4})")

# Período vigente: "02 AGO a 02 SET" → fechamento é a segunda data
RE_PERIODO_VIGENTE = re.compile(
    r"Período vigente:\s*(\d{2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\s+a\s+"
    r"(\d{2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)"
)

# Cartão: linha com símbolos (bullets) + 4 dígitos no início
RE_CARTAO_NUBANK = re.compile(r"[^0-9a-zA-Z]{0,4}(\d{4})\b")

# Valor monetário: "R$ 7,18", "R$ 231,26", "−R$ 194,75", "-R$ 194,75"
RE_VALOR_R = re.compile(r"R\$\s*([\d.]+,\d{2})")

# "Pagamento recebido −R$ 194,75" → crédito (negativo)
RE_PAGAMENTO_RECEBIDO = re.compile(r"Pagamento recebido\s*[−\-]\s*R\$\s*([\d.]+,\d{2})")

# "Fatura anterior R$ 194,75" → saldo anterior
RE_FATURA_ANTERIOR = re.compile(r"Fatura anterior\s*R\$\s*([\d.]+,\d{2})")

# "Total a pagar R$ 231,26"
RE_TOTAL_A_PAGAR = re.compile(r"Total a pagar\s*R\$\s*([\d.]+,\d{2})")

# "Pagamento mínimo para não ficar em atraso R$ 34,68"
RE_PAGAMENTO_MINIMO = re.compile(r"Pagamento mínimo[^R$]*R\$\s*([\d.]+,\d{2})")


class ParserError(Exception):
    """Falha de parse ou validação — mensagem sempre acionável."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _normalizar_texto(s: str) -> str:
    """Normaliza caracteres Unicode: U+2212 (−) → hífen ASCII."""
    return s.replace("\u2212", "-")


def _parse_valor(valor_str: str) -> Decimal | None:
    """
    Parseia um valor monetário brasileiro.

    Ex: "7,18" → Decimal("7.18"), "−R$ 194,75" → Decimal("-194.75")
    Usa o helper RE_VALOR_NEG para detectar sinal, ou procura −/- antes.
    """
    if not valor_str:
        return None
    s = valor_str.strip()
    # Normaliza U+2212 (−) para hífen
    s = s.replace("\u2212", "-")

    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:]

    # Remove "R$" e espaços
    s = s.replace("R$", "").strip()

    # Formata número brasileiro: 1.234,56 → 1234.56
    s = s.replace(".", "").replace(",", ".")

    try:
        v = Decimal(s)
    except Exception:
        return None
    return -v if neg else v


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

def _extract_header(pages: list[dict]) -> dict:
    """
    Extrai header da fatura Nubank.

    Page 1: vencimento ("Data de vencimento: 09 SET 2026") + fechamento
    ("Período vigente: 02 AGO a 02 SET").
    Page 4 (RESUMO): saldo_anterior, creditos (negativo), debitos,
    total_a_pagar, pagamento_minimo.

    Equação: saldo_anterior - creditos + debitos = total_a_pagar
    (ex: 194.75 - 194.75 + 231.26 = 231.26)
    """
    texto_page1 = " ".join(l["text"] for l in pages[0]["lines"])
    texto_page4 = " ".join(l["text"] for l in pages[3]["lines"])
    texto_completo = texto_page1 + " " + texto_page4

    # --- Fechamento (do período vigente) ---
    # "Período vigente: 02 AGO a 02 SET" → fechamento = 02/09/2026
    fechamento = None
    m_periodo = RE_PERIODO_VIGENTE.search(texto_completo)
    if m_periodo:
        dia_fim = int(m_periodo.group(3))
        mes_fim = MESES[m_periodo.group(4)]
        # O ano: o vencimento tem o ano, usar para inferir
        venc_match = RE_DATA_VENCIMENTO.search(texto_completo)
        if venc_match:
            ano = int(venc_match.group(3))
        else:
            ano = 2026
        fechamento = date(ano, mes_fim, dia_fim)

    # --- Vencimento ---
    vencimento = None
    m_venc = RE_DATA_VENCIMENTO.search(texto_completo)
    if m_venc:
        vencimento = date(int(m_venc.group(3)), MESES[m_venc.group(2)], int(m_venc.group(1)))

    # --- Saldo anterior ---
    saldo_anterior = None
    m = RE_FATURA_ANTERIOR.search(texto_completo)
    if m:
        saldo_anterior = _parse_valor(m.group(1))

    # --- Créditos (Pagamento recebido — sempre negativo no PDF) ---
    creditos = None
    m = RE_PAGAMENTO_RECEBIDO.search(texto_completo)
    if m:
        creditos = abs(_parse_valor(m.group(1)))

    # --- Total de débitos = Total de compras ---
    # "Total de compras de todos os cartões, 02 AGO a 02 SET R$ 231,26"
    debitos = None
    m = re.search(r"Total de compras[^R$]*R\$\s*([\d.]+,\d{2})", texto_completo)
    if m:
        debitos = _parse_valor(m.group(1))

    # --- Total a pagar ---
    total_a_pagar = None
    m = RE_TOTAL_A_PAGAR.search(texto_completo)
    if m:
        total_a_pagar = _parse_valor(m.group(1))

    # Se débitos não foi encontrado, mas temos total + saldo_anterior + creditos
    if debitos is None and total_a_pagar is not None and saldo_anterior is not None and creditos is not None:
        debitos = total_a_pagar - saldo_anterior + creditos

    # --- Pagamento mínimo ---
    pagamento_minimo = None
    m = RE_PAGAMENTO_MINIMO.search(texto_completo)
    if m:
        pagamento_minimo = _parse_valor(m.group(1))

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
# Transações (page 5)
# --------------------------------------------------------------------------

# Linhas que marcam o fim das transações
RE_FIM_TRANSACOES = re.compile(
    r"Saldo restante da fatura anterior|PRÓXIMAS FATURAS|LIMITES DISPONÍVEIS"
)


def _extract_transacoes(pages: list[dict]) -> list[Transacao]:
    """
    Lista plana de transações da página de "TRANSAÇÕES".

    Layout tolerante a duas variações:
    - "12 AGO ·8188 Uneedservicos R$ 7,18" (tudo na mesma linha)
    - "·8188 Uneedservicos R$ 7,18" + data na linha seguinte
    Classificação por palavra: data (DD MMM), cartão (bullets+4 dígitos),
    valor (R$ x), resto = descrição.
    """
    transacoes: list[Transacao] = []

    texto_page1 = " ".join(l["text"] for l in pages[0]["lines"])
    m_venc = RE_DATA_VENCIMENTO.search(texto_page1)
    ano_ref = int(m_venc.group(3)) if m_venc else 2026
    m_periodo = RE_PERIODO_VIGENTE.search(texto_page1)
    mes_fechamento = MESES[m_periodo.group(4)] if m_periodo else 9

    def ano_de(mes: int) -> int:
        return ano_ref - 1 if mes > mes_fechamento else ano_ref

    page_transacoes = None
    for page in pages:
        texto = " ".join(l["text"] for l in page["lines"])
        # A página de transações tem "TRANSAÇÕES" + "DE" (período) + tabela de compras
        if "TRANSAÇÕES" in texto and ("DE " in texto or "DE 02" in texto):
            page_transacoes = page
            break
    if page_transacoes is None:
        raise ParserError("Página de transações não encontrada")

    lines = page_transacoes["lines"]
    cartao_atual = ""
    modo_pagamentos = False
    inicio = False
    i = 0
    while i < len(lines):
        text = _normalizar_texto(lines[i]["text"].strip())
        if not text:
            i += 1
            continue
        if not inicio:
            if "TRANSAÇÕES" in text:
                inicio = True
            i += 1
            continue
        if RE_FIM_TRANSACOES.search(text):
            break

        # Pagamentos: aceita "Pagamento em DD MMM −R$ X" (1 linha) ou
        # "Pagamento em DD MMM" com valor 1-2 linhas abaixo
        if modo_pagamentos or "Pagamentos e Financiamentos" in text:
            modo_pagamentos = True
            m1 = re.search(
                r"Pagamento em\s+(\d{2})\s+([A-Z]{3})\s+-R\$\s*([\d.]+,\d{2})", text)
            if m1:
                dia, mes = int(m1.group(1)), MESES[m1.group(2)]
                transacoes.append(Transacao(
                    data=date(ano_de(mes), mes, dia),
                    descricao=f"Pagamento em {m1.group(1)} {m1.group(2)}",
                    valor=-abs(_parse_valor(m1.group(3))),
                    cartao=""))
                i += 1
                continue
            m2 = re.match(r"^Pagamento em\s+(\d{2})\s+([A-Z]{3})$", text)
            if m2:
                dia, mes = int(m2.group(1)), MESES[m2.group(2)]
                valor = None
                for j in range(i + 1, min(i + 3, len(lines))):
                    t = _normalizar_texto(lines[j]["text"])
                    mv = re.search(r"-R\$\s*([\d.]+,\d{2})", t)
                    if mv:
                        valor = -abs(_parse_valor(mv.group(1)))
                        break
                if valor is None:
                    logger.warning("Pagamento sem valor: %r", text)
                else:
                    transacoes.append(Transacao(
                        data=date(ano_de(mes), mes, dia),
                        descricao=f"Pagamento em {m2.group(1)} {m2.group(2)}",
                        valor=valor, cartao=""))
                i += 1
                continue
            i += 1
            continue

        if "Saldo restante da fatura anterior" in text:
            logger.warning("ignorando 'Saldo restante da fatura anterior' (par +/- que zera)")
            i += 1
            continue

        # ---- Extração por regex na linha inteira ----
        # Layout: "... •8188 descricao R$ 7,18" (ou "12 AGO ·8188 descricao R$ 7,18")
        # O "R$" e o valor podem ser tokens separados no split(), entao usa-se regex no texto
        m_valor = RE_VALOR_R.search(text)
        m_cartao = RE_CARTAO_NUBANK.search(text)

        if m_valor is None:
            i += 1
            continue  # linha sem valor: header/lixo

        # Cartão (se existir)
        if m_cartao:
            cartao_atual = "·" + m_cartao.group(1)

        # Descrição: tudo entre o cartão (ou inicio) e o R$
        if m_cartao:
            descricao_raw = text[m_cartao.end():]
        else:
            descricao_raw = text
        m_val = RE_VALOR_R.search(descricao_raw)
        if m_val:
            descricao = descricao_raw[:m_val.start()].strip()
            valor = _parse_valor(m_val.group(1))
        else:
            i += 1
            continue

        # Data: procurar DD MMM na mesma linha; se nao, na linha seguinte
        data_transacao = None
        m_data = RE_DATA_DDMMM.search(text)
        if m_data:
            # Parse manual (DD MMM)
            dia = int(m_data.group(1))
            mes = MESES[m_data.group(2)]
            data_transacao = date(ano_de(mes), mes, dia)
        elif i + 1 < len(lines):
            nd = _normalizar_texto(lines[i + 1]["text"].strip())
            m_data2 = RE_DATA_DDMMM.match(nd)
            if m_data2:
                dia = int(m_data2.group(1))
                mes = MESES[m_data2.group(2)]
                data_transacao = date(ano_de(mes), mes, dia)
                i += 1  # consume a linha da data

        if data_transacao is None:
            logger.warning("linha sem data: %r", text)
            i += 1
            continue

        if valor is not None:
            transacoes.append(Transacao(
                data=data_transacao,
                descricao=descricao,
                valor=valor,
                cartao=cartao_atual,
            ))
        else:
            logger.warning("valor nao parseado: %r", m_valor.group(0))
        i += 1

    return transacoes

# --------------------------------------------------------------------------
# Validação
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

    # 2. Equação do header: saldo_anterior - creditos + debitos = total_a_pagar
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

@registrar("nubank", "2026-09")
def parse_nubank_2026_09(extractor_output: dict) -> Fatura:
    """
    Parser para faturas Nubank (layout 2026-09).

    Args:
        extractor_output: output de extract_text() — dict com chave "pages".

    Returns:
        Fatura — conforme schemas.py.
    """
    pages = extractor_output.get("pages", [])
    if not pages:
        raise ParserError("PDF vazio ou sem páginas")

    if len(pages) < 5:
        raise ParserError(f"Fatura Nubank com {len(pages)} páginas, esperado >= 5")

    header = _extract_header(pages)
    transacoes = _extract_transacoes(pages)

    fatura = Fatura(
        banco="nubank",
        modelo="2026-09",
        fechamento=header["fechamento"],
        vencimento=header["vencimento"],
        total_a_pagar=header["total_a_pagar"],
        pagamento_minimo=header["pagamento_minimo"],
        transacoes=transacoes,
    )
    _validate(fatura, header)
    return fatura
