"""
Categorização determinística por palavra-chave — sem ML.

PRECEDÊNCIA (maior vence):
    1. override manual global   (data/overrides.json)
    2. regras do usuário        (data/categorias.json) — primeira que casar vence
    3. "Outros"                 (fallback implícito, nunca é uma regra)

Regras e overrides NÃO vivem mais hardcoded no código: são arquivos JSON
editáveis em data/. O SEED_REGRAS abaixo é usado UMA ÚNICA VEZ para criar
data/categorias.json quando ele ainda não existe — depois disso, o arquivo
é a fonte da verdade.

Chave de override: normalizar_descricao(descricao) — assim, recategorizar
uma transação ensina o sistema para TODAS as faturas (passadas, via
POST /recategorizar, e futuras, via parse/categorizar).

Arquivos:
    data/categorias.json  {"regras": [{"categoria": "X", "padroes": ["Y"]}, ...]}
    data/overrides.json   {"MERCADO*MERCADOLIVRE": "Supermercado/Mercado", ...}

Uso:
    from categorizer import categorizar
    fatura = categorizar(fatura)  # modifica in-place

    from categorizer import carregar_regras, carregar_overrides
    regras, overrides = carregar_regras(), carregar_overrides()
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import cast

from schemas import Fatura

logger = logging.getLogger("categorizer")

# --- Caminhos (data/ — mesma base de faturas.jsonl) ---
DATA_DIR = Path("data")
CATEGORIAS_PATH = DATA_DIR / "categorias.json"
OVERRIDES_PATH = DATA_DIR / "overrides.json"

# Categoria de fallback quando nenhuma regra casa.
OUTROS = "Outros"

# ---------------------------------------------------------------------------
# SEED — espelho das antigas REGRAS hardcoded (ordem importa).
# Usado UMA vez: só para criar data/categorias.json quando ausente.
# "Outros" NÃO entra aqui: é o fallback implícito do matching.
# ---------------------------------------------------------------------------
SEED_REGRAS: list[dict] = [
    {"categoria": "Pagamento da fatura", "padroes": ["PAGAMENTO DE FATURA"]},
    {"categoria": "Estorno/Ajuste", "padroes": ["AJUSTE A CREDITO"]},
    {"categoria": "Academia/Wellness", "padroes": ["WELLHUB"]},
    {"categoria": "Farmacia", "padroes": ["RAIA DROGASIL", "DROGASIL", "PANVEL"]},
    {"categoria": "Veterinario", "padroes": ["CLINICA VETERINARIA"]},
    {"categoria": "Saude/Podologia", "padroes": ["PODOMAX", "IL BARBUTO"]},
    {"categoria": "Transporte/Apps", "padroes": ["99FOOD", "99*", "99app", "99app *99app", "Dl*Uberrides"]},
    {"categoria": "Restaurante/Cafe", "padroes": [
        "NONO CAFE", "NONOCAFE", "4BEANS", "BEANSCOFFEE",
        "BOCCA LUPO", "BOCCALUPO", "EAT ME", "EATME",
        "RESTAURANTE", "CAFE BISTRO", "CAFEBISTRO", "COLHERADA",
        "EVEREST INN", "DOM MARTIELLO", "MINI KALZONE", "R COMENDADOR",
        "MERCEARIA KIMURA",
    ]},
    {"categoria": "Bar", "padroes": ["DG BARBER", "DEEP LOUNGE BAR", "JANELA BAR"]},
    {"categoria": "Supermercado/Mercado", "padroes": ["MERCADOLIVRE", "MERCEARIA", "PANIFICADORA", "CASA*CARNES"]},
    {"categoria": "Contas/Servicos", "padroes": ["ESTAB. PARCEL. DE COMPRA"]},
    {"categoria": "Compras online", "padroes": [
        "AMAZON", "SHOPEE", "SHEIN", "SHEINCOM",
        "TERABYTE", "DREAMBOX", "GOGOCLOSET", "CHICO REI",
        "LUZESDACI", "DAISO", "ALARGADORES", "GROUPTXRL",
        "AFEBOIKOI", "CASABRASI", "AGBECOMER", "LHCOMPROD",
        "BTWOODLTD", "STERILAIR",
    ]},
]


# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------

_ESPACOS = re.compile(r"\s+")


def normalizar_descricao(s: str) -> str:
    """
    Normaliza uma descrição para uso como chave de override.

    upper + strip + colapso de espaços múltiplos em 1.

    >>> normalizar_descricao("Mercado*MercadoLivre  ")
    'MERCADO*MERCADOLIVRE'
    """
    if not isinstance(s, str):
        return ""
    return _ESPACOS.sub(" ", s).strip().upper()


# ---------------------------------------------------------------------------
# I/O dos arquivos de configuração (data/)
# ---------------------------------------------------------------------------

def _resolver(path: Path | str | None, default: Path) -> Path:
    """Resolve o path no momento da chamada (permite monkeypatch nos testes)."""
    return default if path is None else Path(path)


def _ler_json(path: Path) -> dict | list | None:
    """Lê JSON. Retorna None se o arquivo não existe. Levanta ValueError se corrompido."""
    if not Path(path).exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} contém JSON inválido: {e}") from e
    except OSError as e:
        raise ValueError(f"Erro lendo {path}: {e}") from e


def _escrever_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- Regras ---

def validar_regras(regras) -> list[dict]:
    """
    Valida a lista de regras. Levanta ValueError se inválida.

    Formato aceito: [{"categoria": str não-vazio, "padroes": [str não-vazio, ...]}, ...]
    com pelo menos 1 padrão por regra.
    """
    if not isinstance(regras, list):
        raise ValueError("'regras' deve ser uma lista")
    if not regras:
        raise ValueError("'regras' não pode ser uma lista vazia")

    for i, regra in enumerate(regras):
        if not isinstance(regra, dict):
            raise ValueError(f"regra[{i}] deve ser um objeto")
        categoria = regra.get("categoria")
        if not isinstance(categoria, str) or not categoria.strip():
            raise ValueError(f"regra[{i}].categoria deve ser string não-vazia")
        padroes = regra.get("padroes")
        if not isinstance(padroes, list) or not padroes:
            raise ValueError(f"regra[{i}].padroes deve ser lista com pelo menos 1 item")
        for j, padrao in enumerate(padroes):
            if not isinstance(padrao, str) or not padrao.strip():
                raise ValueError(f"regra[{i}].padroes[{j}] deve ser string não-vazia")
    return regras


def carregar_regras(path: Path | str | None = None) -> list[dict]:
    """
    Carrega as regras de data/categorias.json.

    Se o arquivo não existir, cria-o com SEED_REGRAS (uso único) e retorna o seed.
    Aceita tanto {"regras": [...]} quanto uma lista pura (tolerância).
    """
    path = _resolver(path, CATEGORIAS_PATH)
    data = _ler_json(path)

    if data is None:
        logger.info("categorias.json ausente em %s — criando com o seed", path)
        salvar_regras(SEED_REGRAS, path)
        return copy.deepcopy(SEED_REGRAS)

    regras = data.get("regras") if isinstance(data, dict) else data
    return cast("list[dict]", validar_regras(regras))


def salvar_regras(regras: list[dict], path: Path | str | None = None) -> None:
    """Grava {"regras": [...]} em data/categorias.json."""
    _escrever_json(_resolver(path, CATEGORIAS_PATH), {"regras": regras})


# --- Overrides ---

def carregar_overrides(path: Path | str | None = None) -> dict[str, str]:
    """
    Carrega os overrides de data/overrides.json.

    Leitura tolerante: arquivo ausente → {}. Entradas não-str são ignoradas.
    """
    path = _resolver(path, OVERRIDES_PATH)
    data = _ler_json(path)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} deve conter um objeto JSON ({{descricao_normalizada: categoria}})")

    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def salvar_overrides(overrides: dict[str, str], path: Path | str | None = None) -> None:
    """Grava {descricao_normalizada: categoria} em data/overrides.json."""
    _escrever_json(_resolver(path, OVERRIDES_PATH), dict(overrides))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _compilar(regras: list[dict]) -> list[tuple[str, list[re.Pattern]]]:
    """Compila os padrões uma vez por lote (evita recompilação por transação)."""
    return [
        (
            regra["categoria"],
            [re.compile(re.escape(p), re.IGNORECASE) for p in (regra.get("padroes") or [])],
        )
        for regra in regras
    ]


def _casar(compiladas: list[tuple[str, list[re.Pattern]]], descricao: str) -> str:
    """Primeira regra cuja substring casar vence. Sem casar → OUTROS."""
    for categoria, padroes in compiladas:
        for pat in padroes:
            if pat.search(descricao):
                return categoria
    return OUTROS


def _categorizar_descricao(descricao: str, regras: list[dict] | None = None) -> str:
    """
    Categoria de uma descrição SÓ por regras (sem overrides). Fallback "Outros".

    Mantido para uso direto/compat; o fluxo completo (override > regras) é
    categorizar_descricao() / categorizar().
    """
    if regras is None:
        regras = carregar_regras()
    return _casar(_compilar(regras), descricao)


def categorizar_descricao(
    descricao: str,
    regras: list[dict] | None = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """
    Categoria final de UMA descrição, aplicando a precedência completa.

    override manual global > regras > "Outros".
    """
    if regras is None:
        regras = carregar_regras()
    if overrides is None:
        overrides = carregar_overrides()

    chave = normalizar_descricao(descricao)
    if chave in overrides:
        return overrides[chave]

    return _casar(_compilar(regras), descricao)


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------

def categorizar(
    fatura: Fatura,
    regras: list[dict] | None = None,
    overrides: dict[str, str] | None = None,
) -> Fatura:
    """
    Aplica categorização determinística a todas as transações da fatura.

    Precedência por transação: override global > regras > "Outros".

    Regras e overrides são carregados UMA vez por chamada (ou recebidos por
    parâmetro, para reprocessamento em lote sem reler disco a cada fatura).

    Modifica `fatura.transacoes[i].categoria` in-place e retorna a mesma
    instância.
    """
    if regras is None:
        regras = carregar_regras()
    if overrides is None:
        overrides = carregar_overrides()

    compiladas = _compilar(regras)

    for t in fatura.transacoes:
        chave = normalizar_descricao(t.descricao)
        if chave in overrides:
            t.categoria = overrides[chave]
        else:
            t.categoria = _casar(compiladas, t.descricao)

    return fatura
