"""
API HTTP para fatura-viewer (FastAPI).

Uso interno de casa — sem autenticação.

Endpoints:
  POST   /faturas          — upload de PDF, parseia e persiste
  GET    /faturas          — lista resumida de faturas
  GET    /faturas/{id}     — retorna fatura completa
  DELETE /faturas/{id}     — remove fatura + PDF
  GET    /parsers          — lista parsers registrados
  POST   /inferir-banco    — sugere o banco pelos marcadores de texto do PDF

  GET    /categorias       — regras de categorização (data/categorias.json)
  PUT    /categorias       — substitui as regras (valida; 422 se inválido)
  GET    /overrides        — overrides manuais globais (data/overrides.json)
  PUT    /overrides/{desc} — grava override da descrição normalizada
  DELETE /overrides/{desc} — remove override (404 se não existir)
  GET    /recorrentes      — gastos recorrentes (data/recorrentes.json)
  PUT    /recorrentes/{desc}    — marca descrição como recorrente (flag global)
  DELETE /recorrentes/{desc}    — desmarca (404 se não estiver marcada)
  POST   /recategorizar    — re-aplica regras+overrides em todas as faturas

Run:
  uvicorn api:app --reload
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

# Importar parsers para registrar no registry
from parsers import *  # noqa: F401, F403 — side-effect: registra parsers
from extractors.pdf_extractor import extract_text
from inferencia import inferir_banco
from registry import get_parser, parsers_registrados
from schemas import Fatura, Transacao, fatura_para_dict
from parsers.sofisa_2026_09 import ParserError

from categorizer import (
    aplicar_overrides,
    carregar_overrides,
    carregar_recorrentes,
    carregar_regras,
    categorizar,
    normalizar_descricao,
    salvar_overrides,
    salvar_recorrentes,
    salvar_regras,
    validar_regras,
)

from comparativo import calcular_comparativo, calcular_consolidado

logger = logging.getLogger("api")

app = FastAPI(title="fatura-viewer")

# --- Caminhos (todos derivados de DATA_DIR) ---
DATA_DIR = Path("data")
UPLOADS_DIR = DATA_DIR / "uploads"
JSONL_PATH = DATA_DIR / "faturas.jsonl"
CATEGORIAS_PATH = DATA_DIR / "categorias.json"
OVERRIDES_PATH = DATA_DIR / "overrides.json"
RECORRENTES_PATH = DATA_DIR / "recorrentes.json"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers de persistência
# ---------------------------------------------------------------------------

def _carregar_registros() -> list[dict]:
    """Lê data/faturas.jsonl e retorna lista de registros (retorna [] se não existe)."""
    if not JSONL_PATH.exists():
        return []
    lines = JSONL_PATH.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _salvar_registros(regs: list[dict]) -> None:
    """Regrava o JSONL com a lista dada (uma linha JSON por registro)."""
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for reg in regs:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")


def _encontrar_registro(fatura_id: str) -> dict | None:
    """Procura um registro pelo ID no JSONL. Retorna o dict ou None."""
    for reg in _carregar_registros():
        if reg["id"] == fatura_id:
            return reg
    return None


def _erro422(mensagem: str) -> JSONResponse:
    """Resposta de erro no padrão da API: {"erro": ...}."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"erro": mensagem},
    )


def _com_ids(payload: dict) -> dict:
    """
    Garante "id" em cada transação do payload (tolerância a faturas salvas
    antes da introdução do campo: id = índice na lista lida).
    """
    payload = dict(payload)
    transacoes = []
    for i, t in enumerate(payload.get("transacoes") or []):
        t = dict(t)
        if not isinstance(t.get("id"), int):
            t["id"] = i
        transacoes.append(t)
    payload["transacoes"] = transacoes
    return payload


def _dict_para_fatura(payload: dict) -> Fatura | None:
    """
    Reconstrói uma Fatura a partir do payload persistido no JSONL.

    Retorna None (com log) se o payload estiver corrompido/incompleto, para
    que um registro ruim não invalide o lote inteiro de recategorização.
    """
    try:
        transacoes = [
            Transacao(
                data=date.fromisoformat(t["data"]),
                descricao=t["descricao"],
                valor=Decimal(str(t["valor"])),
                parcela_atual=t.get("parcela_atual"),
                parcela_total=t.get("parcela_total"),
                moeda=t.get("moeda", "BRL"),
                categoria=t.get("categoria"),
                cartao=t.get("cartao", ""),
                id=t.get("id") if isinstance(t.get("id"), int) else i,
            )
            for i, t in enumerate(payload.get("transacoes") or [])
        ]
        return Fatura(
            banco=payload["banco"],
            modelo=payload["modelo"],
            fechamento=date.fromisoformat(payload["fechamento"]),
            vencimento=date.fromisoformat(payload["vencimento"]),
            total_a_pagar=Decimal(str(payload["total_a_pagar"])),
            pagamento_minimo=Decimal(str(payload["pagamento_minimo"])),
            transacoes=transacoes,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as e:
        logger.warning("Payload inválido ao reconstruir fatura: %s", e)
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/comparativo")
async def comparativo():
    """Comparação mês a mês + anomalias sobre todas as faturas salvas."""
    return calcular_comparativo(_carregar_registros())

@app.get("/consolidado")
async def consolidado():
    """Visão consolidada: histórico por mês + projeção de todas as faturas."""
    recorrentes = set(carregar_recorrentes(RECORRENTES_PATH) or {})
    return calcular_consolidado(_carregar_registros(), recorrentes)

@app.post("/faturas")
async def upload_fatura(arquivo: UploadFile = File(...), banco: str = Form(...), versao: str = "latest"):
    filename = arquivo.filename or ""
    if not filename.lower().endswith(".pdf"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": "Arquivo deve ser PDF (.pdf)"},
        )

    # Salvar upload
    fatura_id = str(uuid.uuid4())
    file_path = UPLOADS_DIR / f"{fatura_id}.pdf"
    try:
        content = await arquivo.read()
        file_path.write_bytes(content)
    except Exception as e:
        logger.exception("Erro salvando upload: %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": f"Erro salvando arquivo: {e}"},
        )

    # Extract + parse
    try:
        extractor_output = extract_text(file_path)
        parse_fn = get_parser(banco, versao)  # pode levantar ValueError
        fatura = parse_fn(extractor_output)     # pode levantar ParserError
    except ParserError as e:
        logger.exception("ParserError: %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": str(e)},
        )
    except ValueError as e:
        logger.exception("Parser não encontrado: %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": str(e)},
        )
    except Exception as e:
        logger.exception("Erro processando PDF (corrompido?): %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": f"PDF corrompido ou ilegível: {e}"},
        )

    # Aplicar categorização determinística (regras do usuário + overrides globais)
    # Overrides lidos 1x por request e reaproveitados na resposta.
    overrides_em_uso = carregar_overrides(OVERRIDES_PATH)
    try:
        fatura = categorizar(
            fatura,
            regras=carregar_regras(CATEGORIAS_PATH),
            overrides=overrides_em_uso,
        )
    except ValueError as e:
        logger.exception("Configuração de categorias inválida: %s", e)
        return _erro422(f"Erro na configuração de categorias: {e}")

    payload = fatura_para_dict(fatura)
    payload["id"] = fatura_id

    # Persistir em JSONL
    registro = {
        "id": fatura_id,
        "criado_em": datetime.now(timezone.utc).isoformat(),
        "banco": payload["banco"],
        "modelo": payload["modelo"],
        "payload": payload,
    }
    regras = _carregar_registros()
    regras.append(registro)
    _salvar_registros(regras)

    # Resposta: overrides aplicados na leitura (cópia — o JSONL guarda o parse)
    resposta = dict(payload, transacoes=[dict(t) for t in payload["transacoes"]])
    return aplicar_overrides(resposta, overrides_em_uso)


@app.get("/faturas")
async def listar_faturas():
    faturas = []
    for reg in _carregar_registros():
        p = reg["payload"]
        faturas.append({
            "id": reg["id"],
            "banco": p["banco"],
            "modelo": p["modelo"],
            "fechamento": p["fechamento"],
            "total_a_pagar": p["total_a_pagar"],
        })
    # Mais recentes primeiro
    faturas.reverse()
    return faturas


@app.get("/faturas/{fatura_id}")
async def get_fatura(fatura_id: str):
    reg = _encontrar_registro(fatura_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada")
    # Overrides aplicados na LEITURA: ensinamento vale na hora, sem precisar de
    # POST /recategorizar. Lidos 1x por request e SEMPRE pelo path da API
    # (OVERRIDES_PATH deriva de DATA_DIR); _com_ids já devolve cópia, então o
    # payload persistido não é tocado.
    return aplicar_overrides(_com_ids(reg["payload"]), carregar_overrides(OVERRIDES_PATH))


@app.delete("/faturas/{fatura_id}")
async def delete_fatura(fatura_id: str):
    reg = _encontrar_registro(fatura_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada")

    # Remover do JSONL (regrava sem o registro)
    regras = [r for r in _carregar_registros() if r["id"] != fatura_id]
    _salvar_registros(regras)

    # Apagar PDF do disco
    pdf_path = UPLOADS_DIR / f"{fatura_id}.pdf"
    pdf_path.unlink(missing_ok=True)

    return {"ok": True, "id": fatura_id}


@app.get("/parsers")
async def listar_parsers():
    return parsers_registrados()


@app.post("/inferir-banco")
async def inferir_banco_endpoint(arquivo: UploadFile = File(...)):
    """
    Recebe um PDF e infere o banco pelos marcadores de texto do header.

    Só uma SUGESTÃO para o frontend: quem decide o parser continua sendo o
    campo `banco` do POST /faturas. Mesma extração do POST /faturas
    (`extract_text`, que recebe PATH — daí o arquivo temporário); o texto das
    páginas é concatenado e passado à inferência.

    Retorna {"banco": "sofisa"} ou {"banco": null}. 422 (padrão {"erro": ...})
    se não for PDF ou se o PDF estiver ilegível — o frontend trata qualquer
    não-200 como "não foi possível identificar" e não bloqueia o upload.
    """
    if not (arquivo.filename or "").lower().endswith(".pdf"):
        return _erro422("Envie um PDF (.pdf)")

    conteudo = await arquivo.read()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(conteudo)
        tmp_path = Path(tmp.name)

    try:
        extractor_output = extract_text(tmp_path)
    except Exception as e:
        logger.exception("Erro extraindo PDF para inferência: %s", e)
        return _erro422(f"PDF corrompido ou ilegível: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

    texto = "\n".join(p.get("text") or "" for p in extractor_output.get("pages") or [])
    bancos = sorted(parsers_registrados().keys())   # mesma fonte do GET /parsers
    return {"banco": inferir_banco(texto, bancos)}


# ---------------------------------------------------------------------------
# Categorias (regras editáveis) e overrides globais
# ---------------------------------------------------------------------------

@app.get("/categorias")
async def get_categorias():
    """Conteúdo de data/categorias.json (cria o seed na primeira chamada)."""
    return {"regras": carregar_regras(CATEGORIAS_PATH)}


@app.put("/categorias")
async def put_categorias(request: Request):
    """
    Substitui as regras. Aceita {"regras": [...]} (formato do GET) ou lista pura.

    422 {"erro": ...} se o JSON for inválido.
    """
    try:
        body = await request.json()
    except Exception:
        return _erro422("JSON inválido no corpo da requisição")

    regras_raw = body.get("regras") if isinstance(body, dict) else body
    try:
        regras = validar_regras(regras_raw)
    except ValueError as e:
        return _erro422(str(e))

    salvar_regras(regras, CATEGORIAS_PATH)
    return {"regras": regras}


@app.get("/overrides")
async def get_overrides():
    """Overrides manuais globais: {descricao_normalizada: categoria}."""
    return carregar_overrides(OVERRIDES_PATH)


@app.put("/overrides/{descricao}")
async def put_override(descricao: str, request: Request):
    """
    Grava (ou substitui) o override da descrição normalizada.

    Body: {"categoria": "X"} — 422 se 'categoria' for vazia.
    """
    try:
        body = await request.json()
    except Exception:
        return _erro422("JSON inválido no corpo da requisição")

    categoria = body.get("categoria") if isinstance(body, dict) else None
    if not isinstance(categoria, str) or not categoria.strip():
        return _erro422("Campo 'categoria' é obrigatório e deve ser string não-vazia")

    chave = normalizar_descricao(descricao)
    if not chave:
        return _erro422("Descrição vazia")

    overrides = carregar_overrides(OVERRIDES_PATH)
    overrides[chave] = categoria.strip()
    salvar_overrides(overrides, OVERRIDES_PATH)

    return {"chave": chave, "categoria": overrides[chave]}


@app.delete("/overrides/{descricao}")
async def delete_override(descricao: str):
    """Remove o override da descrição normalizada. 404 se não existir."""
    chave = normalizar_descricao(descricao)
    overrides = carregar_overrides(OVERRIDES_PATH)

    if chave not in overrides:
        raise HTTPException(status_code=404, detail="Override não encontrado")

    del overrides[chave]
    salvar_overrides(overrides, OVERRIDES_PATH)
    return {"ok": True, "chave": chave}


@app.get("/recorrentes")
async def get_recorrentes():
    """
    Gastos recorrentes: {descricao_normalizada: true}.

    Flag GLOBAL do usuário (mesma chave normalizada dos overrides), ortogonal a
    categoria e a parcela: um item que se repete todo mês (WELLHUB, Apple Bill,
    Spotify...). A flag NÃO é injetada nas transações do payload — o cliente
    consulta este endpoint e cruza localmente, para que o estado do usuário não
    divirja do que está gravado no jsonl.
    """
    return carregar_recorrentes(RECORRENTES_PATH)


@app.put("/recorrentes/{descricao}")
async def put_recorrente(descricao: str):
    """
    Marca a descrição normalizada como gasto recorrente. Sem body.

    200 {"ok": true, "chave": "<descricao normalizada>"}.
    """
    chave = normalizar_descricao(descricao)
    if not chave:
        return _erro422("Descrição vazia")

    recorrentes = carregar_recorrentes(RECORRENTES_PATH)
    recorrentes[chave] = True
    salvar_recorrentes(recorrentes, RECORRENTES_PATH)

    return {"ok": True, "chave": chave}


@app.delete("/recorrentes/{descricao}")
async def delete_recorrente(descricao: str):
    """Desmarca a descrição. 404 se não estiver marcada como recorrente."""
    chave = normalizar_descricao(descricao)
    recorrentes = carregar_recorrentes(RECORRENTES_PATH)

    if chave not in recorrentes:
        raise HTTPException(status_code=404, detail="Recorrente não encontrado")

    del recorrentes[chave]
    salvar_recorrentes(recorrentes, RECORRENTES_PATH)
    return {"ok": True, "chave": chave}


@app.post("/recategorizar")
async def recategorizar():
    """
    Re-aplica regras + overrides em TODAS as faturas de data/faturas.jsonl.

    Regra/override são lidos UMA vez para o lote inteiro.
    """
    regras = carregar_regras(CATEGORIAS_PATH)
    overrides = carregar_overrides(OVERRIDES_PATH)

    registros = _carregar_registros()
    recategorizadas = 0

    for reg in registros:
        payload = reg.get("payload")
        if not isinstance(payload, dict):
            continue
        fatura = _dict_para_fatura(payload)
        if fatura is None:
            continue

        categorizar(fatura, regras=regras, overrides=overrides)
        payload["transacoes"] = fatura_para_dict(fatura)["transacoes"]
        reg["payload"] = payload
        recategorizadas += 1

    _salvar_registros(registros)
    return {"recategorizadas": recategorizadas}


# Servir frontend estático (após todas as rotas da API)
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
