"""
API HTTP para fatura-viewer (FastAPI).

Uso interno de casa — sem autenticação.

Endpoints:
  POST   /faturas          — upload de PDF, parseia e persiste
  GET    /faturas          — lista resumida de faturas
  GET    /faturas/{id}     — retorna fatura completa
  DELETE /faturas/{id}     — remove fatura + PDF
  GET    /parsers          — lista parsers registrados

Run:
  uvicorn api:app --reload
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

# Importar parsers para registrar no registry
from parsers import *  # noqa: F401, F403 — side-effect: registra parsers
from extractors.pdf_extractor import extract_text
from registry import get_parser, parsers_registrados
from schemas import fatura_para_dict
from parsers.sofisa_2026_09 import ParserError

logger = logging.getLogger("api")

app = FastAPI(title="fatura-viewer")

# --- Caminhos ---
DATA_DIR = Path("data")
UPLOADS_DIR = DATA_DIR / "uploads"
JSONL_PATH = DATA_DIR / "faturas.jsonl"

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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

    # Aplicar categorização determinística
    from categorizer import categorizar
    fatura = categorizar(fatura)

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

    return payload


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
    return reg["payload"]


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


# Servir frontend estático (após todas as rotas da API)
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
