"""
Parser para faturas do Cartão de Crédito Mercado Pago — layout "2026-09".

Características do layout Mercado Pago:
- Duas seções de movimentações: "Movimentações na fatura" (pagamentos e
  encargos) e por cartão "Cartão Visa [••••••••••9599]".
- Datas só com DD/MM (sem ano) — ano inferido pelo fechamento:
  mês > mês do fechamento → ano anterior (compras de dez/fev no ciclo).
- Parcelamento como sufixo "Parcela N de M" (já na ordem certa).
- Valores no formato brasileiro (1.985,06 / 353,55).
- Header: "Total a pagar R$ X Vence em", "Pagamento mínimo", datas e
  limites na seção "Seu cartão de crédito".
- Validação: soma das transações da seção do cartão == "Consumos de ...".
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from registry import registrar
from schemas import Fatura, Transacao

logger = logging.getLogger(__name__)

# Token de valor brasileiro: 353,55 | 1.985,06
RE_VALOR = re.compile(r"([\d]{1,3}(?:\.\d{3})*,\d{2})")

# Linha de transação: "03/09 DESCRIÇÃO R$ 23,99" ou
# "28/09 DESCRIÇÃO Parcela 12 de 12 R$ 121,35"
RE_TRANSACAO = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)(?:\s+Parcela\s+(\d+)\s+de\s+(\d+))?"
    r"\s+R\$\s*" + RE_VALOR.pattern + r"$"
)

# Seção do cartão: "Cartão Visa [************9599]"
RE_CARTAO = re.compile(r"Cartão\s+(Visa|Mastercard|Elo)\s*\[\*+(\d{4})\]")


class ParserError(Exception):
    """Falha de parse ou validação — mensagem sempre acionável."""


def _valor(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def _data_ano(ddmm: str, fechamento: date) -> date:
    """DD/MM sem ano → date. Mês maior que o do fechamento = ano anterior."""
    d, m = int(ddmm[:2]), int(ddmm[3:5])
    ano = fechamento.year - 1 if m > fechamento.month else fechamento.year
    return date(ano, m, d)


def _extract_header(full: str) -> dict:
    header: dict = {}

    m = re.search(r"Fechamento da fatura\s*(\d{2}/\d{2}/\d{4})", full)
    if not m:
        raise ParserError("Fechamento da fatura não encontrado")
    header["fechamento"] = date.fromisoformat(
        m.group(1)[6:10] + "-" + m.group(1)[3:5] + "-" + m.group(1)[:2]
    )

    # "Vence em" é cabeçalho de COLUNA; a data vem na linha seguinte,
    # depois do valor de "Total a pagar".
    m = re.search(r"Vence em[^\n]*\n[^\n]*?(\d{2}/\d{2}/\d{4})", full)
    if not m:
        raise ParserError("Vencimento não encontrado")
    header["vencimento"] = date.fromisoformat(
        m.group(1)[6:10] + "-" + m.group(1)[3:5] + "-" + m.group(1)[:2]
    )

    # O "R$ 395,01" abre a linha SEGUINTE ao cabeçalho de colunas.
    m = re.search(r"Total a pagar[^\n]*\nR\$\s*" + RE_VALOR.pattern, full)
    if not m:
        raise ParserError("Total a pagar não encontrado")
    header["total_a_pagar"] = _valor(m.group(1))

    # Duas ocorrências de R$ na linha ("1 + 15x R$ 44,73 R$ 74,11"):
    # o mínimo é o ÚLTIMO valor.
    m = re.search(r"Pagamento mínimo\s*\n([^\n]*)", full)
    if not m:
        raise ParserError("Pagamento mínimo não encontrado")
    valores = RE_VALOR.findall(m.group(1))
    if not valores:
        raise ParserError("Pagamento mínimo não encontrado")
    header["pagamento_minimo"] = _valor(valores[-1])

    # Resumo — "Consumos de 10/08 a 09/09 R$ 377,54"
    m = re.search(
        r"Consumos de\s*\d{2}/\d{2}\s*a\s*\d{2}/\d{2}\s*R\$\s*"
        + RE_VALOR.pattern, full
    )
    if not m:
        raise ParserError("Resumo 'Consumos de' não encontrado")
    header["consumos"] = _valor(m.group(1))

    return header


def _extract_transacoes(lines: list[str], fechamento: date) -> list[Transacao]:
    transacoes: list[Transacao] = []
    cartao = ""  # seção atual: "" = movimentações na fatura

    for linha in lines:
        m_cartao = RE_CARTAO.search(linha)
        if m_cartao:
            cartao = f"{m_cartao.group(1)} ••{m_cartao.group(2)}"
            continue

        m = RE_TRANSACAO.match(linha.strip())
        if not m:
            continue
        ddmm, descricao = m.group(1), m.group(2).strip()
        pa, pt = m.group(3), m.group(4)

        transacoes.append(Transacao(
            data=_data_ano(ddmm, fechamento),
            descricao=descricao,
            valor=_valor(m.group(5)),
            parcela_atual=int(pa) if pa else None,
            parcela_total=int(pt) if pt else None,
            cartao=cartao,
        ))
    return transacoes


def _validar(fatura: Fatura, consumos: Decimal) -> None:
    soma_cartao = sum(
        (t.valor for t in fatura.transacoes if t.cartao != ""),
        Decimal("0"),
    )
    if soma_cartao != consumos:
        raise ParserError(
            f"Soma das compras ({soma_cartao}) != resumo de consumos "
            f"({consumos}) — layout mudou?"
        )
    logger.info(
        "Validação OK: %d transações, consumos %s", 
        len(fatura.transacoes), consumos,
    )


@registrar("mercadopago", "2026-09")
def parse_mercadopago_2026_09(extractor_output: dict) -> Fatura:
    full = "\n".join(p["text"] for p in extractor_output["pages"])
    lines = [l["text"] for p in extractor_output["pages"] for l in p["lines"]]

    header = _extract_header(full)
    transacoes = _extract_transacoes(lines, header["fechamento"])

    if not transacoes:
        raise ParserError("Nenhuma transação extraída")

    fatura = Fatura(
        banco="mercadopago",
        modelo="2026-09",
        fechamento=header["fechamento"],
        vencimento=header["vencimento"],
        total_a_pagar=header["total_a_pagar"],
        pagamento_minimo=header["pagamento_minimo"],
        transacoes=transacoes,
    )
    _validar(fatura, header["consumos"])
    return fatura
