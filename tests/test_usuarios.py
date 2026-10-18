"""
Testes da feature de usuários (tag por fatura).

Cobre:
- CRUD completo de /api/usuarios (GET/POST/DELETE), 409 duplicado
  (comparação normalizada: strip + casefold)
- 404 no DELETE de usuário inexistente
- DELETE desmarca (usuario=null) todas as faturas vinculadas
- PATCH /api/usuarios/{nome} renomeia + retaga atomicamente (200/404/409/422);
  rename para o MESMO nome → no-op success (retag=0), não 409
- PATCH /api/faturas/{id} retag válido / 422 com nome inexistente / null limpa
- POST /api/faturas (multipart) com e sem usuario; GET lista e detail expõem usuario
- Isolação: tudo em tmp_path via monkeypatch — NUNCA toca data/ real

Modelo de isolamento: tests/test_personalizacao.py (fixture `isolado`).
O fixture monkeypatcha DATA_DIR, JSONL_PATH, UPLOADS_DIR, CATEGORIAS_PATH,
OVERRIDES_PATH, RECORRENTES_PATH E USUARIOS_PATH todos para tmp_path.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import api
import categorizer
from api import app


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolado(tmp_path, monkeypatch):
    """Redireciona TODOS os paths de data/ para tmp_path (nada toca data/ real)."""
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "JSONL_PATH", tmp_path / "faturas.jsonl")
    monkeypatch.setattr(api, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(api, "OVERRIDES_PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(api, "RECORRENTES_PATH", tmp_path / "recorrentes.json")
    monkeypatch.setattr(api, "USUARIOS_PATH", tmp_path / "usuarios.json")
    # categorizer.py (paths usados por carregar_regras etc. no caminho do upload)
    monkeypatch.setattr(categorizer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(categorizer, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(categorizer, "OVERRIDES_PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(categorizer, "RECORRENTES_PATH", tmp_path / "recorrentes.json")
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def client(isolado):
    return TestClient(app)


def _transacao(descricao: str, valor: str = "10.00", categoria=None) -> dict:
    return {
        "data": "2026-09-01",
        "descricao": descricao,
        "valor": valor,
        "parcela_atual": None,
        "parcela_total": None,
        "moeda": "BRL",
        "categoria": categoria,
        "cartao": "1234",
    }


def _payload(transacoes: list[dict]) -> dict:
    return {
        "banco": "teste",
        "modelo": "2026-09",
        "fechamento": "2026-09-15",
        "vencimento": "2026-09-20",
        "total_a_pagar": "30.00",
        "pagamento_minimo": "3.00",
        "transacoes": transacoes,
    }


def _gravar_jsonl(path, payloads: list[dict], usuario=None) -> list[str]:
    """Grava registros no JSONL com campo usuario opcional (default None).

    Os registros antigos não têm o campo → None (ausente). Grava IDs estáveis.
    """
    ids = []
    with open(path, "w", encoding="utf-8") as f:
        for i, p in enumerate(payloads):
            reg = {
                "id": f"fat-{i}",
                "criado_em": "2026-09-18T00:00:00+00:00",
                "banco": p["banco"],
                "modelo": p["modelo"],
                "pdf_hash": f"hash-{i}",
                "usuario": usuario,
                "payload": p,
            }
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
            ids.append(reg["id"])
    return ids


# ---------------------------------------------------------------------------
# 1. CRUD de /api/usuarios (seed vazio + case-insensitive)
# ---------------------------------------------------------------------------

def test_usuarios_crud(client, isolado):
    """GET cria usuarios.json vazio; POST cria; DELETE remove; 404 inexistente."""
    # GET sem arquivo → cria seed vazio
    resp = client.get("/api/usuarios")
    assert resp.status_code == 200
    assert resp.json() == {"usuarios": []}
    assert (isolado / "usuarios.json").read_text() == "[]\n"

    # POST cria
    resp = client.post("/api/usuarios", json={"nome": "Jhonatan"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "nome": "Jhonatan"}

    resp = client.post("/api/usuarios", json={"nome": "Mariana"})
    assert resp.status_code == 200

    # GET lista ordenada
    resp = client.get("/api/usuarios")
    assert resp.json() == {"usuarios": ["Jhonatan", "Mariana"]}

    # 409 duplicado (case-insensitive, strip)
    for dup in ["Jhonatan", "  jhonatan  ", "JHONATAN"]:
        resp = client.post("/api/usuarios", json={"nome": dup})
        assert resp.status_code == 409, f"duplicado aceito: {dup!r}"

    # 422 nome vazio
    assert client.post("/api/usuarios", json={"nome": "   "}).status_code == 422
    assert client.post("/api/usuarios", json={"nome": ""}).status_code == 422
    assert "erro" in client.post("/api/usuarios", json={"nome": ""}).json()

    # DELETE remove
    resp = client.delete("/api/usuarios/" + quote("Jhonatan"))
    assert resp.status_code == 200
    assert resp.json()["nome"] == "Jhonatan"
    assert client.get("/api/usuarios").json() == {"usuarios": ["Mariana"]}

    # 404 delete inexistente
    assert client.delete("/api/usuarios/" + quote("NAO")).status_code == 404

    # segunda tentativa de delete já removido → 404
    assert client.delete("/api/usuarios/" + quote("JHONATAN")).status_code == 404


# ---------------------------------------------------------------------------
# 2. DELETE desmarca faturas vinculadas
# ---------------------------------------------------------------------------

def test_delete_usuario_desmarca_faturas(client, isolado):
    """Deletar um usuário zera usuario=None em todas as faturas com a tag."""
    # Cadastra usuário e grava 1 fatura taggeada + 1 sem tag
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    _gravar_jsonl(isolado / "faturas.jsonl", [
        _payload([_transacao("UMA COISA")]),
        _payload([_transacao("OUTRA COISA")]),
    ], usuario="Jhonatan")

    # confirma o tag existente no GET /faturas
    faturas = client.get("/api/faturas").json()
    assert faturas[0]["usuario"] == "Jhonatan"
    assert faturas[1]["usuario"] == "Jhonatan"

    # deleta o usuário → todas as faturas desmarcam
    resp = client.delete("/api/usuarios/" + quote("Jhonatan"))
    assert resp.status_code == 200
    assert resp.json()["desmarcadas"] is True

    # GET /faturas agora mostra usuario=None
    faturas = client.get("/api/faturas").json()
    assert faturas[0]["usuario"] is None
    assert faturas[1]["usuario"] is None
    # usuarios.json vazio
    assert client.get("/api/usuarios").json() == {"usuarios": []}

    # jsonl no disco reflete usuario=None (JSONL = 1 registro por linha)
    linhas = (isolado / "faturas.jsonl").read_text(encoding="utf-8").strip().splitlines()
    regs = [json.loads(l) for l in linhas]
    assert all(reg["usuario"] is None for reg in regs)


# ---------------------------------------------------------------------------
# 3. PATCH /api/faturas/{id} retag
# ---------------------------------------------------------------------------

def test_patch_usuario(client, isolado):
    """PATCH retag válido / 422 com nome inexistente / null limpa / 404."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    client.post("/api/usuarios", json={"nome": "Mariana"})

    ids = _gravar_jsonl(isolado / "faturas.jsonl", [
        _payload([_transacao("COISA")]),
    ])
    fid = ids[0]

    # PATCH com nome inexistente → 422 (comparação normalizada)
    resp = client.patch(f"/api/faturas/{fid}", json={"usuario": "NaoExiste"})
    assert resp.status_code == 422
    assert "erro" in resp.json()
    # tag não foi aplicada
    assert client.get(f"/api/faturas/{fid}").json()["usuario"] is None

    # PATCH válido (match case-insensitive)
    resp = client.patch(f"/api/faturas/{fid}", json={"usuario": "  jhonatan  "})
    assert resp.status_code == 200
    assert resp.json()["usuario"] == "Jhonatan"  # canônico do cadastrado

    # GET /faturas/{id} reflete a tag
    assert client.get(f"/api/faturas/{fid}").json()["usuario"] == "Jhonatan"

    # Retag para outro usuário existente
    resp = client.patch(f"/api/faturas/{fid}", json={"usuario": "Mariana"})
    assert resp.status_code == 200
    assert resp.json()["usuario"] == "Mariana"

    # null limpa a tag
    resp = client.patch(f"/api/faturas/{fid}", json={"usuario": None})
    assert resp.status_code == 200
    assert resp.json()["usuario"] is None
    assert client.get(f"/api/faturas/{fid}").json()["usuario"] is None

    # 404 fatura inexistente
    assert client.patch("/api/faturas/NAO-EXISTE", json={"usuario": "Jhonatan"}).status_code == 404

    # PATCH sem o campo usuario: nada muda (preserva)
    client.patch(f"/api/faturas/{fid}", json={"usuario": "Jhonatan"})
    resp = client.patch(f"/api/faturas/{fid}", json={"outro": "campo"})
    assert resp.status_code == 200
    assert resp.json()["usuario"] == "Jhonatan"


# ---------------------------------------------------------------------------
# 4. Upload multipart com/sen sem usuario
# ---------------------------------------------------------------------------

FATURA_PDF = "in/Fatura.pdf"


def _upload(client, banco="sofisa", usuario=None, **data):
    """Faz upload de in/Fatura.pdf e devolve o JSON da resposta."""
    with open(FATURA_PDF, "rb") as f:
        files = {"arquivo": ("Fatura.pdf", f, "application/pdf")}
        data_all = {"banco": banco, **data}
        if usuario is not None:
            data_all["usuario"] = usuario
        resp = client.post("/api/faturas", files=files, data=data_all)
    assert resp.status_code == 200, resp.text
    assert "Traceback" not in resp.text
    return resp.json()


def _upload_bruto(client, banco="sofisa", usuario=None, **data):
    """Upload sem assertar status — para testar 409/422."""
    with open(FATURA_PDF, "rb") as f:
        files = {"arquivo": ("Fatura.pdf", f, "application/pdf")}
        data_all = {"banco": banco, **data}
        if usuario is not None:
            data_all["usuario"] = usuario
        return client.post("/api/faturas", files=files, data=data_all)


@pytest.mark.slow
def test_upload_com_usuario(client, isolado):
    """Upload com usuario → tag persiste e aparece no GET /faturas e /faturas/{id}."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    data = _upload(client, usuario="Jhonatan")
    fid = data["id"]

    # resposta do upload inclui a tag
    assert data["usuario"] == "Jhonatan"

    # GET /faturas lista expõe usuario
    faturas = client.get("/api/faturas").json()
    assert len(faturas) == 1
    assert faturas[0]["usuario"] == "Jhonatan"

    # GET /faturas/{id} expõe usuario
    completo = client.get(f"/api/faturas/{fid}").json()
    assert completo["usuario"] == "Jhonatan"
    assert completo["banco"] == "sofisa"

    # jsonl persiste o campo top-level usuario
    reg = json.loads((isolado / "faturas.jsonl").read_text(encoding="utf-8").strip())
    assert reg["usuario"] == "Jhonatan"
    # o payload interno NÃO carrega usuario
    assert "usuario" not in reg["payload"]

    # reupload do mesmo PDF → 409 (dedupe intacto; chave natural NÃO inclui usuario)
    resp = _upload_bruto(client, usuario="Jhonatan")
    assert resp.status_code == 409, "dedupe quebrou: reupload do mesmo PDF deveria 409"


@pytest.mark.slow
def test_upload_sem_usuario(client, isolado):
    """Upload sem usuario → tag null (ausente/vazio)."""
    data = _upload(client)
    assert data["usuario"] is None

    faturas = client.get("/api/faturas").json()
    assert faturas[0]["usuario"] is None

    completo = client.get(f"/api/faturas/{data['id']}").json()
    assert completo["usuario"] is None


@pytest.mark.slow
def test_upload_usuario_inexistente_422(client, isolado):
    """Upload com usuario inexistente → 422 e nada persistido."""
    resp = _upload_bruto(client, usuario="Inexistente")
    assert resp.status_code == 422
    assert "erro" in resp.json()
    # nada persistido
    assert not (isolado / "faturas.jsonl").exists()
    assert not list((isolado / "uploads").glob("*.pdf"))


# ---------------------------------------------------------------------------
# 5. Renomear usuário (PATCH /api/usuarios/{nome})
# ---------------------------------------------------------------------------
# Decisão: renomear para o MESMO nome canônico → no-op SUCCESS (retag=0).
# Não 409: o próprio nome não é um "conflito", é um re-submit que deve ser
# idempotente (clientes reenviam o nome atual). Documentado no HANDOFF.

def test_patch_usuario_renomeia_e_retaga(client, isolado):
    """Rename retaga todas as faturas e atualiza usuarios.json atomicamente."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    ids = _gravar_jsonl(isolado / "faturas.jsonl", [
        _payload([_transacao("COISA A")]),
        _payload([_transacao("COISA B")]),
    ], usuario="Jhonatan")
    fid = ids[0]

    # Renomeia Jhonatan → Júlia
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "Júlia"})
    assert resp.status_code == 200
    assert resp.json() == {"usuario": "Júlia", "retag": 2}

    # usuarios.json reflete o novo nome (e não tem Jhonatan)
    assert client.get("/api/usuarios").json() == {"usuarios": ["Júlia"]}

    # GET /faturas expõe a nova tag em TODAS as faturas atreladas
    faturas = client.get("/api/faturas").json()
    assert faturas[0]["usuario"] == "Júlia"
    assert faturas[1]["usuario"] == "Júlia"

    # GET /faturas/{id} também
    assert client.get(f"/api/faturas/{fid}").json()["usuario"] == "Júlia"

    # JSONL no disco retagado atomicamente
    regs = [json.loads(l) for l in (isolado / "faturas.jsonl").read_text().splitlines()]
    assert all(reg["usuario"] == "Júlia" for reg in regs)


def test_patch_usuario_404(client, isolado):
    """PATCH de usuário inexistente → 404, nada alterado."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    resp = client.patch("/api/usuarios/" + quote("NaoExiste"), json={"novo_nome": "X"})
    assert resp.status_code == 404
    assert client.get("/api/usuarios").json() == {"usuarios": ["Jhonatan"]}


def test_patch_usuario_409_confling(client, isolado):
    """Rename para nome já existente (outro) → 409, nada alterado."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    client.post("/api/usuarios", json={"nome": "Mariana"})
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "Mariana"})
    assert resp.status_code == 409
    # NADA mudou: Jhonatan e Mariana continuam como estavam
    assert client.get("/api/usuarios").json() == {"usuarios": ["Jhonatan", "Mariana"]}
    # case-insensitive também é conflito
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": " mariana "})
    assert resp.status_code == 409


def test_patch_usuario_422_nome_vazio(client, isolado):
    """novo_nome vazio → 422, nada alterado."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "   "})
    assert resp.status_code == 422
    assert "erro" in resp.json()
    assert client.get("/api/usuarios").json() == {"usuarios": ["Jhonatan"]}


def test_patch_usuario_mesmo_nome_noop(client, isolado):
    """Rename para o MESMO nome canônico → no-op success (retag=0), não 409."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    # "Jhonatan" (mesmo nome exato)
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "Jhonatan"})
    assert resp.status_code == 200
    assert resp.json() == {"usuario": "Jhonatan", "retag": 0}
    # case-insensitive: "JHONATAN" caso-canônico já existe → mesmo nome
    resp = client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "  JHONATAN  "})
    assert resp.status_code == 200
    assert resp.json()["retag"] == 0
    assert client.get("/api/usuarios").json() == {"usuarios": ["Jhonatan"]}


def test_patch_usuario_atomico_se_novo_nome_invalido(client, isolado):
    """Em caso de 409/422/404, usuarios.json e JSONL permanecem inalterados."""
    client.post("/api/usuarios", json={"nome": "Jhonatan"})
    client.post("/api/usuarios", json={"nome": "Mariana"})
    _gravar_jsonl(isolado / "faturas.jsonl", [
        _payload([_transacao("COISA")]),
    ], usuario="Jhonatan")
    jsonl_antes = (isolado / "faturas.jsonl").read_text()
    usuarios_antes = client.get("/api/usuarios").json()

    # tenta rename conflitante → 409
    client.patch("/api/usuarios/" + quote("Jhonatan"), json={"novo_nome": "Mariana"})

    # nada mudou (atomicidade)
    assert client.get("/api/usuarios").json() == usuarios_antes
    assert (isolado / "faturas.jsonl").read_text() == jsonl_antes
