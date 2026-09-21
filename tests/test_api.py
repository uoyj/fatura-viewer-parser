"""
Testes para api.py (FastAPI).

Padrão da suíte: os testes que sobem TestClient e fazem upload de PDF são
@pytest.mark.slow (rode `pytest -m "not slow"` para o ciclo rápido) e foram
fundidos para reduzir o número de uploads — cada upload reparseia o PDF real.

Cobre:
- POST /api/faturas: upload OK (campos, transações, PDF no disco, JSONL, sem traceback)
- POST /api/faturas: versão explícita
- POST /api/faturas: banco inexistente / arquivo não-PDF → 422 {"erro": ...}
- GET /api/parsers, GET /faturas (vazio), GET /faturas/{id}, 404
- DELETE /api/faturas/{id}: remove JSONL + PDF do disco; 404 quando inexistente
- Override manual valendo IMEDIATAMENTE no GET (sem /recategorizar)

ISOLAMENTO OBRIGATORIO: os testes NUNCA tocam no data/ real do usuario.
O fixture monkeypatcha UPLOADS_DIR e JSONL_PATH para um tmp_path
que o pytest apaga ao final da sessão — PDFs de teste nunca chegam
ao data/ do usuario.
"""

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

# Importar o modulo api para monkeypatch nos paths
import api
from api import app

slow = pytest.mark.slow  # sobe TestClient com upload de PDF

FATURA_PDF = "in/Fatura.pdf"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """
    Cliente TestClient isolado.

    tmp_path e apagado pelo pytest ao final — PDFs de teste nunca
    chegam ao data/ do usuario.
    """
    # Redireciona uploads e jsonl para diretorios temporarios
    monkeypatch.setattr(api, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "JSONL_PATH", tmp_path / "faturas.jsonl")
    # Regras/overrides de categorizacao tambem (upload categoriza as transacoes)
    monkeypatch.setattr(api, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(api, "OVERRIDES_PATH", tmp_path / "overrides.json")
    api.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    return TestClient(app)


def _upload(client, banco: str = "sofisa", **data) -> dict:
    """Faz upload de in/Fatura.pdf e devolve o JSON da resposta (sem traceback)."""
    with open(FATURA_PDF, "rb") as f:
        resp = client.post(
            "/api/faturas",
            files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
            data={"banco": banco, **data},
        )
    assert resp.status_code == 200, resp.text
    assert "Traceback" not in resp.text
    assert "File " not in resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Upload + leitura
# ---------------------------------------------------------------------------

@slow
def test_upload_persiste_e_leitura_completa(client):
    """Upload OK (campos + persistência) e leitura por id/lista."""
    data = _upload(client)
    assert data["banco"] == "sofisa"
    assert data["modelo"] == "2026-09"
    assert data["fechamento"] == "2026-09-15"
    assert data["vencimento"] == "2026-09-20"
    assert data["total_a_pagar"] == "6727.55"
    assert data["pagamento_minimo"] == "672.76"
    assert "id" in data
    assert len(data["transacoes"]) == 96

    fatura_id = data["id"]

    # PDF persistido no uploads do tmp_path (nao no data real)
    assert (api.UPLOADS_DIR / f"{fatura_id}.pdf").exists(), "PDF nao persistido"

    # JSONL persistido
    assert api.JSONL_PATH.exists()
    linhas = api.JSONL_PATH.read_text().strip().splitlines()
    assert len(linhas) == 1
    reg = json.loads(linhas[0])
    assert reg["id"] == fatura_id
    assert "criado_em" in reg and "payload" in reg
    assert reg["payload"]["banco"] == "sofisa"

    # GET /faturas lista 1 item
    faturas = client.get("/api/faturas").json()
    assert len(faturas) == 1
    assert faturas[0]["id"] == fatura_id
    assert faturas[0]["banco"] == "sofisa"
    assert faturas[0]["fechamento"] == "2026-09-15"
    assert faturas[0]["total_a_pagar"] == "6727.55"

    # GET /faturas/{id} devolve o payload completo
    completo = client.get(f"/api/faturas/{fatura_id}").json()
    assert completo["id"] == fatura_id
    assert completo["banco"] == "sofisa"
    assert len(completo["transacoes"]) == 96

    # id inexistente -> 404
    assert client.get(
        "/api/faturas/00000000-0000-0000-0000-000000000000").status_code == 404


@slow
def test_upload_com_versao(client):
    """Versão explícita é aceita (o _upload já garante ausência de traceback)."""
    data = _upload(client, versao="2026-09")
    assert data["modelo"] == "2026-09"


@slow
def test_upload_rejeitado_422(client):
    """Banco inexistente e arquivo não-PDF → 422 {"erro": ...} (sem traceback)."""
    # banco inexistente
    with open(FATURA_PDF, "rb") as f:
        resp = client.post(
            "/api/faturas",
            files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
            data={"banco": "naoexiste"},
        )
    assert resp.status_code == 422
    assert "erro" in resp.json()
    assert "naoexiste" in resp.json()["erro"]
    assert "Traceback" not in resp.text

    # arquivo .txt
    resp = client.post(
        "/api/faturas",
        files={"arquivo": ("test.txt", b"hello world", "text/plain")},
        data={"banco": "sofisa"},
    )
    assert resp.status_code == 422
    assert "pdf" in resp.json()["erro"].lower()


def test_get_parsers_e_faturas_vazio(client):
    """Sem upload: /parsers lista os bancos e /faturas é [] (jsonl ausente)."""
    resp = client.get("/api/parsers")
    assert resp.status_code == 200
    data = resp.json()
    assert "sofisa" in data
    assert "2026-09" in data["sofisa"]

    resp = client.get("/api/faturas")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@slow
def test_delete_remove_jsonl_e_pdf(client):
    """DELETE remove do JSONL + PDF do disco; 404 quando não existe."""
    fatura_id = _upload(client)["id"]

    resp = client.delete(f"/api/faturas/{fatura_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": fatura_id}

    assert not (api.UPLOADS_DIR / f"{fatura_id}.pdf").exists(), "PDF nao removido"
    assert client.get(f"/api/faturas/{fatura_id}").status_code == 404
    assert client.get("/api/faturas").json() == []

    assert client.delete(
        "/api/faturas/00000000-0000-0000-0000-000000000000").status_code == 404


# ---------------------------------------------------------------------------
# Override na leitura
# ---------------------------------------------------------------------------

# 'Compra a Vista NONO CAFE' aparece 9x na fatura sofisa com a mesma
# descricao -> o override por descricao normalizada muda as 9 de uma vez.
DESCRICAO = "Compra a Vista NONO CAFE"
CATEGORIA_REGRA = "Restaurante/Cafe"
CATEGORIA_OVERRIDE = "Teste"
OVERRIDE_URL = f"/api/overrides/{quote(DESCRICAO, safe='*')}"


def _cats_no_get(client, fid) -> list[str]:
    transacoes = client.get(f"/api/faturas/{fid}").json()["transacoes"]
    return [t["categoria"] for t in transacoes if t["descricao"] == DESCRICAO]


@slow
def test_override_reflete_no_get_e_jsonl_fica_intacto(client):
    """Override vale IMEDIATAMENTE no GET, sem /recategorizar; JSONL mantém o parse."""
    fid = _upload(client)["id"]

    # sem override: categoria vem da REGRA e /overrides está vazio
    assert client.get("/api/overrides").json() == {}
    antes = _cats_no_get(client, fid)
    assert len(antes) == 9
    assert set(antes) == {CATEGORIA_REGRA}

    # PUT do override — sem chamar /recategorizar em momento algum
    resp = client.put(OVERRIDE_URL, json={"categoria": CATEGORIA_OVERRIDE})
    assert resp.status_code == 200
    assert resp.json()["chave"] == DESCRICAO.upper()

    depois = _cats_no_get(client, fid)
    assert len(depois) == 9, "todas as linhas com a mesma descricao devem mudar"
    assert set(depois) == {CATEGORIA_OVERRIDE}

    # o JSONL continua com o resultado do PARSE (override é aplicado na leitura)
    reg = json.loads(api.JSONL_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
    no_disco = [t["categoria"] for t in reg["payload"]["transacoes"]
                if t["descricao"] == DESCRICAO]
    assert set(no_disco) == {CATEGORIA_REGRA}


@slow
def test_override_delete_e_upload_response(client):
    """DELETE do override volta à categoria original e o POST também o reflete."""
    fid = _upload(client)["id"]
    assert client.put(OVERRIDE_URL,
                      json={"categoria": CATEGORIA_OVERRIDE}).status_code == 200
    assert set(_cats_no_get(client, fid)) == {CATEGORIA_OVERRIDE}

    # a resposta do POST /faturas também passa pelos overrides
    data = _upload(client)
    cats = [t["categoria"] for t in data["transacoes"] if t["descricao"] == DESCRICAO]
    assert cats and set(cats) == {CATEGORIA_OVERRIDE}

    resp = client.delete(OVERRIDE_URL)
    assert resp.status_code == 200
    assert set(_cats_no_get(client, fid)) == {CATEGORIA_REGRA}
