"""
Categorização determinística por palavra-chave — sem ML.

Aplica regras de substring (case-insensitive) sobre descricao da transacao.
Primeira regra que casar vence. Se nenhuma casar, categoria = "Outros".

REGRAS baseadas nos nomes reais extraídos da Fatura.pdf de exemplo.
Editáveis — basta modificar a lista abaixo.

Uso:
    from categorizer import categorizar
    fatura = categorizar(fatura)  # modifica in-place
"""

import re
from schemas import Fatura

# Lista de (categoria, padroes). Ordem importa — primeira que casa vence.
# Matching: substring case-insensitive da descricao.
REGRAS: list[tuple[str, list[str]]] = [
    ("Pagamento da fatura", ["PAGAMENTO DE FATURA"]),
    ("Estorno/Ajuste", ["AJUSTE A CREDITO"]),
    ("Academia/Wellness", ["WELLHUB"]),
    ("Farmacia", ["RAIA DROGASIL", "DROGASIL", "PANVEL"]),
    ("Veterinario", ["CLINICA VETERINARIA"]),
    ("Saude/Podologia", ["PODOMAX", "IL BARBUTO"]),
    ("Transporte/Apps", ["99FOOD", "99*"]),
    ("Restaurante/Cafe", [
        "NONO CAFE", "NONOCAFE", "4BEANS", "BEANSCOFFEE",
        "BOCCA LUPO", "BOCCALUPO", "EAT ME", "EATME",
        "RESTAURANTE", "CAFE BISTRO", "CAFEBISTRO", "COLHERADA",
        "EVEREST INN", "DOM MARTIELLO", "MINI KALZONE", "R COMENDADOR",
        "MERCEARIA KIMURA",
    ]),
    ("Bar", ["DG BARBER", "DEEP LOUNGE BAR", "JANELA BAR"]),
    ("Supermercado/Mercado", ["MERCADOLIVRE", "MERCEARIA", "PANIFICADORA", "CASA*CARNES"]),
    ("Contas/Servicos", ["ESTAB. PARCEL. DE COMPRA"]),
    ("Compras online", [
        "AMAZON", "SHOPEE", "SHEIN", "SHEINCOM",
        "TERABYTE", "DREAMBOX", "GOGOCLOSET", "CHICO REI",
        "LUZESDACI", "DAISO", "ALARGADORES", "GROUPTXRL",
        "AFEBOIKOI", "CASABRASI", "AGBECOMER", "LHCOMPROD",
        "BTWOODLTD", "STERILAIR",
    ]),
    ("Outros", []),  # fallback
]

# Compila padrões para eficiência (não é requisito, mas evita recompilação)
_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (cat, [re.compile(re.escape(p), re.IGNORECASE) for p in pads])
    for cat, pads in REGRAS
]


def _categorizar_descricao(descricao: str) -> str:
    """Retorna a categoria para uma descrição de transação."""
    for cat, patterns in _COMPILED:
        if cat == "Outros":
            continue
        for pat in patterns:
            if pat.search(descricao):
                return cat
    return "Outros"


def categorizar(fatura: Fatura) -> Fatura:
    """
    Aplica categorização determinística a todas as transações da fatura.

    Modifica `fatura.transacoes[i].categoria` in-place e retorna a mesma
    instância.
    """
    for t in fatura.transacoes:
        t.categoria = _categorizar_descricao(t.descricao)
    return fatura
