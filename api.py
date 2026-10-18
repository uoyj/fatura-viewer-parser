"""
API HTTP para fatura-viewer (FastAPI).

Uso interno de casa — sem autenticação.

Endpoints:
  POST   /api/faturas          — upload de PDF, parseia e persiste
  GET    /api/faturas          — lista resumida de faturas
  GET    /api/faturas/{id}     — retorna fatura completa
  DELETE /api/faturas/{id}     — remove fatura + PDF
  GET    /api/parsers          — lista parsers registrados
  POST   /api/inferir-banco    — sugere o banco pelos marcadores de texto do PDF

  GET    /api/categorias       — regras de categorização (data/categorias.json)
  PUT    /api/categorias       — substitui as regras (valida; 422 se inválido)
  GET    /api/overrides        — overrides manuais globais (data/overrides.json)
  PUT    /api/overrides/{desc} — grava override da descrição normalizada
  DELETE /api/overrides/{desc} — remove override (404 se não existir)
  GET    /api/recorrentes      — gastos recorrentes (data/recorrentes.json)
  PUT    /api/recorrentes/{desc}    — marca descrição como recorrente (flag global)
  DELETE /api/recorrentes/{desc}    — desmarca (404 se não estiver marcada)
  POST   /api/recategorizar    — re-aplica regras+overrides em todas as faturas
  GET    /api/usuarios         — lista de nomes (data/usuarios.json), ordenada
  POST   /api/usuarios         — cria usuário (409 se já existir, case-insensitive)
  PATCH  /api/usuarios/{nome}  — renomeia usuário + retaga faturas (409 se conflito)
  DELETE /api/usuarios/{nome}  — deleta + desmarca (usuario=null) faturas taggeadas
  PATCH  /api/faturas/{id}     — retag de usuário: {"usuario": "nome"|null}

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
from dedupe import chave_natural, encontrar_duplicata, fingerprint_pdf

logger = logging.getLogger("api")

app = FastAPI(title="fatura-viewer")

# --- Caminhos (todos derivados de DATA_DIR) ---
DATA_DIR = Path("data")
UPLOADS_DIR = DATA_DIR / "uploads"
JSONL_PATH = DATA_DIR / "faturas.jsonl"
CATEGORIAS_PATH = DATA_DIR / "categorias.json"
OVERRIDES_PATH = DATA_DIR / "overrides.json"
RECORRENTES_PATH = DATA_DIR / "recorrentes.json"
USUARIOS_PATH = DATA_DIR / "usuarios.json"

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
# Usuários (tags por fatura — nível fatura, não cartão)
# ---------------------------------------------------------------------------
# data/usuarios.json: lista JSON de nomes (["nome", ...]), semeada vazia quando
# ausente — MESMO padrão de categorias.json (arquivo é a fonte da verdade após
# a primeira criação). Não é autenticação: apenas uma tag opcional por fatura.
_SEED_USUARIOS: list[str] = []


def _normalizar_usuario(nome: str) -> str:
    """Strip + casefold — usado para comparação/dedup de nomes de usuário."""
    return (nome or "").strip().casefold()


def _escrever_json_usuarios(path: Path, data) -> None:
    """Grava JSON em data/usuarios.json (cria diretório pai)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _ler_json_usuarios(path: Path) -> list | None:
    """Lê usuarios.json. Retorna None se ausente, levanta ValueError se corrompido."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} contém JSON inválido: {e}") from e
    except OSError as e:
        raise ValueError(f"Erro lendo {path}: {e}") from e
    return data


def _carregar_usuarios(path: Path | str | None = None) -> list[str]:
    """
    Carrega a lista de usuários de data/usuarios.json.

    Se o arquivo não existir, cria-o com [] (seed vazio — MESMO padrão de
    categorias.json). Depois disso, o arquivo é a fonte da verdade.
    """
    path = USUARIOS_PATH if path is None else Path(path)
    data = _ler_json_usuarios(path)
    if data is None:
        _escrever_json_usuarios(path, list(_SEED_USUARIOS))
        return list(_SEED_USUARIOS)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"{path} deve conter uma lista JSON de nomes (strings)")
    return list(data)


def _salvar_usuarios(usuarios: list[str], path: Path | str | None = None) -> None:
    """Grava a lista de nomes em data/usuarios.json."""
    path = USUARIOS_PATH if path is None else Path(path)
    _escrever_json_usuarios(path, list(usuarios))


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

@app.get("/api/comparativo")
async def comparativo():
    """Comparação mês a mês + anomalias sobre todas as faturas salvas."""
    return calcular_comparativo(_carregar_registros())

@app.get("/api/consolidado")
async def consolidado():
    """Visão consolidada: histórico por mês + projeção de todas as faturas."""
    recorrentes = set(carregar_recorrentes(RECORRENTES_PATH) or {})
    return calcular_consolidado(_carregar_registros(), recorrentes)

@app.post("/api/faturas")
async def upload_fatura(arquivo: UploadFile = File(...), banco: str = Form(...), versao: str = "latest", usuario: str = Form(None)):
    filename = arquivo.filename or ""
    if not filename.lower().endswith(".pdf"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": "Arquivo deve ser PDF (.pdf)"},
        )

    # Ler bytes do upload (antes de qualquer I/O de persistência)
    try:
        content = await arquivo.read()
    except Exception as e:
        logger.exception("Erro lendo upload: %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": f"Erro lendo arquivo: {e}"},
        )

    # Extract + parse (de arquivo temporário; dedupe precisa da chave natural)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        extractor_output = extract_text(tmp_path)
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
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)

    # Usuario (tag por fatura, opcional) — validado ANTES de tocar o disco:
    # ausente/vazio → null; informado e inexistente → 422 (sem gravar PDF).
    if usuario and usuario.strip():
        usuarios = _carregar_usuarios(USUARIOS_PATH)
        norm = _normalizar_usuario(usuario)
        if not any(_normalizar_usuario(u) == norm for u in usuarios):
            return _erro422(f"Usuário inexistente: {usuario.strip()}")
        alvo = next(u for u in usuarios if _normalizar_usuario(u) == norm)
    else:
        alvo = None

    # Dedupe: verifica hash e chave natural antes de persistir
    payload = fatura_para_dict(fatura)
    fp = fingerprint_pdf(content)
    chave = chave_natural(payload)
    duplicata = encontrar_duplicata(fp, chave, _carregar_registros())
    if duplicata:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "fatura_duplicada",
                "fatura_id": duplicata["id"],
                "banco": payload["banco"],
                "fechamento": payload["fechamento"],
            },
        )

    # Salvar upload
    fatura_id = str(uuid.uuid4())
    file_path = UPLOADS_DIR / f"{fatura_id}.pdf"
    try:
        file_path.write_bytes(content)
    except Exception as e:
        logger.exception("Erro salvando upload: %s", e)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"erro": f"Erro salvando arquivo: {e}"},
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
        "pdf_hash": fp,
        "usuario": alvo,
        "payload": payload,
    }
    regras = _carregar_registros()
    regras.append(registro)
    _salvar_registros(regras)

    # Resposta: overrides aplicados na leitura (cópia — o JSONL guarda o parse)
    resposta = dict(payload, transacoes=[dict(t) for t in payload["transacoes"]])
    resposta["usuario"] = alvo
    return aplicar_overrides(resposta, overrides_em_uso)


@app.get("/api/faturas")
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
            "usuario": reg.get("usuario"),
        })
    # Mais recentes primeiro
    faturas.reverse()
    return faturas


@app.get("/api/faturas/{fatura_id}")
async def get_fatura(fatura_id: str):
    reg = _encontrar_registro(fatura_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada")
    # Overrides aplicados na LEITURA: ensinamento vale na hora, sem precisar de
    # POST /recategorizar. Lidos 1x por request e SEMPRE pelo path da API
    # (OVERRIDES_PATH deriva de DATA_DIR); _com_ids já devolve cópia, então o
    # payload persistido não é tocado.
    payload = aplicar_overrides(_com_ids(reg["payload"]), carregar_overrides(OVERRIDES_PATH))
    payload["id"] = reg["id"]
    payload["usuario"] = reg.get("usuario")
    return payload


@app.delete("/api/faturas/{fatura_id}")
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


@app.get("/api/parsers")
async def listar_parsers():
    return parsers_registrados()


@app.post("/api/inferir-banco")
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

@app.get("/api/categorias")
async def get_categorias():
    """Conteúdo de data/categorias.json (cria o seed na primeira chamada)."""
    return {"regras": carregar_regras(CATEGORIAS_PATH)}


@app.put("/api/categorias")
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


@app.get("/api/overrides")
async def get_overrides():
    """Overrides manuais globais: {descricao_normalizada: categoria}."""
    return carregar_overrides(OVERRIDES_PATH)


@app.put("/api/overrides/{descricao}")
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


@app.delete("/api/overrides/{descricao}")
async def delete_override(descricao: str):
    """Remove o override da descrição normalizada. 404 se não existir."""
    chave = normalizar_descricao(descricao)
    overrides = carregar_overrides(OVERRIDES_PATH)

    if chave not in overrides:
        raise HTTPException(status_code=404, detail="Override não encontrado")

    del overrides[chave]
    salvar_overrides(overrides, OVERRIDES_PATH)
    return {"ok": True, "chave": chave}


@app.get("/api/recorrentes")
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


@app.put("/api/recorrentes/{descricao}")
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


@app.delete("/api/recorrentes/{descricao}")
async def delete_recorrente(descricao: str):
    """Desmarca a descrição. 404 se não estiver marcada como recorrente."""
    chave = normalizar_descricao(descricao)
    recorrentes = carregar_recorrentes(RECORRENTES_PATH)

    if chave not in recorrentes:
        raise HTTPException(status_code=404, detail="Recorrente não encontrado")

    del recorrentes[chave]
    salvar_recorrentes(recorrentes, RECORRENTES_PATH)
    return {"ok": True, "chave": chave}


# ---------------------------------------------------------------------------
# Usuários (tag por fatura — nível fatura, não cartão)
# ---------------------------------------------------------------------------
# Modelo: 1 usuário por FATURA. Os 3 cartões da Sofisa são do mesmo titular;
# dependentes têm faturas separadas. A tag é um campo opcional top-level no
# registro JSONL (junto de id/criado_em/banco), NÃO entra no payload parseado
# nem na chave natural de dedupe (mesma fatura = mesma fatura). Sem auth:
# retag é um PATCH stateless por quem tem acesso local à API.
@app.get("/api/usuarios")
async def get_usuarios():
    """Lista de nomes de usuário, ordenada. Cria data/usuarios.json (vazio) se ausente."""
    usuarios = _carregar_usuarios(USUARIOS_PATH)
    return {"usuarios": sorted(usuarios)}


@app.post("/api/usuarios")
async def post_usuario(request: Request):
    """
    Cria um usuário. Body: {"nome": "X"}.
    409 se já existir (comparação normalizada: strip + casefold).
    """
    try:
        body = await request.json()
    except Exception:
        return _erro422("JSON inválido no corpo da requisição")
    nome = body.get("nome") if isinstance(body, dict) else None
    if not isinstance(nome, str) or not nome.strip():
        return _erro422("Campo 'nome' é obrigatório e deve ser string não-vazia")

    usuarios = _carregar_usuarios(USUARIOS_PATH)
    norm = _normalizar_usuario(nome)
    if any(_normalizar_usuario(u) == norm for u in usuarios):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Usuário já existe: {nome.strip()}",
        )
    usuarios.append(nome.strip())
    _salvar_usuarios(usuarios, USUARIOS_PATH)
    return {"ok": True, "nome": nome.strip()}


@app.delete("/api/usuarios/{nome}")
async def delete_usuario(nome: str):
    """
    Deleta um usuário. 404 se não existir (comparação normalizada).
    Ao deletar, DESMARCA (usuario=null) todas as faturas com essa tag.
    """
    usuarios = _carregar_usuarios(USUARIOS_PATH)
    norm = _normalizar_usuario(nome)
    alvo = next((u for u in usuarios if _normalizar_usuario(u) == norm), None)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    # Desmarcar todas as faturas com essa tag
    regs = _carregar_registros()
    alteradas = False
    for reg in regs:
        if reg.get("usuario") is not None and _normalizar_usuario(reg["usuario"]) == norm:
            reg["usuario"] = None
            alteradas = True
    if alteradas:
        _salvar_registros(regs)

    usuarios = [u for u in usuarios if _normalizar_usuario(u) != norm]
    _salvar_usuarios(usuarios, USUARIOS_PATH)
    return {"ok": True, "nome": alvo, "desmarcadas": alteradas}


@app.patch("/api/usuarios/{nome}")
async def patch_usuario(nome: str, request: Request):
    """
    Renomear um usuário (tag por fatura). Body: {"novo_nome": "..."}.
    - 404 se {nome} não existir (comparação normalizada);
    - 422 se novo_nome vazio;
    - 409 se novo_nome já existir (case-insensitive, mesmo critério do POST),
      EXCETO auto-rename (mesmo nome canônico) → no-op success (retag=0).

    ATÔMICO: valida tudo antes de persistir — renomeia data/usuarios.json E
    retaga todas as faturas usuario==nome → novo_nome em uma única gravação
    do JSONL. Nunca deixa faturas apontando para nome inexistente.
    Retorna {"usuario": <novo_nome_canonico>, "retag": <n de faturas>}.
    """
    try:
        body = await request.json()
    except Exception:
        return _erro422("JSON inválido no corpo da requisição")
    if not isinstance(body, dict):
        return _erro422("Corpo deve ser um objeto JSON")

    novo_nome = body.get("novo_nome")
    if not isinstance(novo_nome, str) or not novo_nome.strip():
        return _erro422("Campo 'novo_nome' é obrigatório e deve ser string não-vazia")
    novo_nome = novo_nome.strip()

    # 1) Validar tudo ANTES de tocar em qualquer arquivo (atomicidade)
    usuarios = _carregar_usuarios(USUARIOS_PATH)
    norm_atual = _normalizar_usuario(nome)
    alvo_atual = next((u for u in usuarios if _normalizar_usuario(u) == norm_atual), None)
    if alvo_atual is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    norm_novo = _normalizar_usuario(novo_nome)
    alvo_novo = next((u for u in usuarios if _normalizar_usuario(u) == norm_novo), None)
    # Auto-rename (mesmo nome canônico) → no-op; evita 409 frustrante em
    # clientes que reenviam o próprio nome. Documentado no HANDOFF.
    if alvo_novo is not None and alvo_novo != alvo_atual:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Usuário já existe: {alvo_novo}",
        )

    # 2) Persistir ATÔMICO: retag no JSONL primeiro, depois a lista de nomes.
    # Se o JSONL gravar e a lista falhar, o nome novo já existe no JSONL (e a
    # lista é re-criada do seed vazio na próxima leitura — consistente).
    # nome_canonico: preferimos o nome já cadastrado com a mesma grafia
    # (case-insensitive) — evita duplicatas como "Jhonatan" + "jhonatan".
    nome_canonico = alvo_novo if alvo_novo is not None else novo_nome
    regs = _carregar_registros()
    retag = 0
    for reg in regs:
        if reg.get("usuario") is not None and _normalizar_usuario(reg["usuario"]) == norm_atual:
            reg["usuario"] = nome_canonico
            retag += 1
    if retag:
        _salvar_registros(regs)

    usuarios = [nome_canonico if _normalizar_usuario(u) == norm_atual else u for u in usuarios]
    _salvar_usuarios(usuarios, USUARIOS_PATH)

    return {"usuario": nome_canonico, "retag": retag}


@app.patch("/api/faturas/{fatura_id}")
async def patch_fatura(fatura_id: str, request: Request):
    """
    Retag de usuário numa fatura existente. Body: {"usuario": "nome" | null}.
    - nome inexistente em usuarios.json → 422
    - null limpa a tag (usuario=null)
    - sem o campo no body, a tag é preservada (PATCH parcial)
    """
    reg = _encontrar_registro(fatura_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada")

    try:
        body = await request.json()
    except Exception:
        return _erro422("JSON inválido no corpo da requisição")
    if not isinstance(body, dict):
        return _erro422("Corpo deve ser um objeto JSON")

    if "usuario" in body:
        valor = body["usuario"]
        if valor is None:
            reg["usuario"] = None
        elif isinstance(valor, str) and valor.strip():
            usuarios = _carregar_usuarios(USUARIOS_PATH)
            norm = _normalizar_usuario(valor)
            if not any(_normalizar_usuario(u) == norm for u in usuarios):
                return _erro422(f"Usuário inexistente: {valor.strip()}")
            reg["usuario"] = valor.strip()
            # Preserva o registro canônico do usuário já cadastrado
            alvo = next(u for u in usuarios if _normalizar_usuario(u) == norm)
            reg["usuario"] = alvo
        else:
            return _erro422("Campo 'usuario' deve ser string não-vazia ou null")

        # Reescrever o JSONL substituindo o registro pelo id correspondente
        regs = _carregar_registros()
        for i, r in enumerate(regs):
            if r["id"] == reg["id"]:
                regs[i] = reg
                break
        _salvar_registros(regs)
        return {"ok": True, "id": fatura_id, "usuario": reg["usuario"]}

    # PATCH sem o campo usuario: nada a alterar, retorna o estado atual
    return {"ok": True, "id": fatura_id, "usuario": reg.get("usuario")}


@app.post("/api/recategorizar")
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


# Servir frontend estático (SPA com history routing) — registrado por ÚLTIMO.
# Resolução: 1) arquivo real de static/ entregue pelo MESMO StaticFiles de antes
#              (HEAD, ETag/304, Range e a guarda de traversal ficam idênticos);
#           2) senão index.html — o mesmo shell para /view/<id>, /dicionario,
#              /comparativo e /consolidado (a URL é resolvida no cliente).
# As rotas /api/* casam ANTES desta, então não são interceptadas; um /api/*
# inexistente continua 404 (não vira HTML).
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# O 404 do StaticFiles é starlette.exceptions.HTTPException (o HTTPException do
# FastAPI é SUBCLASSE dela — `except fastapi.HTTPException` não pega o raise dele).
from starlette.exceptions import HTTPException as StarletteHTTPException

STATIC_DIR = Path("static")
_arquivos = StaticFiles(directory=str(STATIC_DIR), html=True)   # reuso, não é mount


# methods GET+HEAD: o StaticFiles atendia HEAD (200); só com GET o HEAD viraria
# 405 e mudaria o comportamento de quem faz HEAD nos assets.
from fastapi.responses import FileResponse

@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def spa_fallback(full_path: str):
    arquivo = Path("static") / full_path
    if full_path and arquivo.is_file():
        return FileResponse(arquivo)
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
