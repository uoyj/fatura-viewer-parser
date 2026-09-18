"""
Testes para api.py (FastAPI).

Cobre:
- POST /faturas: upload OK com Fatura.pdf → 200 + JSON válido
- POST /faturas: banco inexistente → 422 {"erro": ...}
- POST /faturas: arquivo .txt → 422 {"erro": ...}
- GET /parsers: retorna dict de bancos
- GET /faturas/{id}: retorna payload completo
- GET /faturas (lista vazia quando jsonl não existe): []
- DELETE /faturas/{id}: remove do JSONL + PDF do disco
- DELETE /faturas/{id} inexistente: 404

ISOLAMENTO OBRIGATORIO: os testes NUNCA tocam no data/ real do usuario.
O fixture monkeypatcha UPLOADS_DIR e JSONL_PATH para um tmp_path
que o pytest apaga ao final da sessão — PDFs de teste nunca chegam
ao data/ do usuario.
"""

import json
import pytest
from fastapi.testclient import TestClient

# Importar o modulo api para monkeypatch nos paths
import api
from api import app

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


class TestUploadFatura:

    def test_upload_ok(self, client):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["banco"] == "sofisa"
        assert data["modelo"] == "2026-09"
        assert data["fechamento"] == "2026-09-15"
        assert data["vencimento"] == "2026-09-20"
        assert data["total_a_pagar"] == "6727.55"
        assert data["pagamento_minimo"] == "672.76"
        assert "id" in data
        assert "transacoes" in data
        assert len(data["transacoes"]) == 96

        # PDF deve existir no uploads do tmp_path (nao no data real)
        fatura_id = data["id"]
        pdf_path = api.UPLOADS_DIR / f"{fatura_id}.pdf"
        assert pdf_path.exists(), "PDF nao persistido em uploads"

    def test_upload_com_versao(self, client):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa", "versao": "2026-09"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["modelo"] == "2026-09"

    def test_banco_inexistente(self, client):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "naoexiste"},
            )
        assert resp.status_code == 422
        body = resp.json()
        assert "erro" in body
        assert "naoexiste" in body["erro"]

    def test_arquivo_nao_pdf(self, client):
        resp = client.post(
            "/faturas",
            files={"arquivo": ("test.txt", b"hello world", "text/plain")},
            data={"banco": "sofisa"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "erro" in body
        assert "pdf" in body["erro"].lower()

    def test_nunca_traceback_no_cliente(self, client):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert resp.status_code == 200
        text = resp.text
        assert "Traceback" not in text
        assert "File " not in text


class TestListagem:

    def test_get_parsers(self, client):
        resp = client.get("/parsers")
        assert resp.status_code == 200
        data = resp.json()
        assert "sofisa" in data
        assert "2026-09" in data["sofisa"]

    def test_get_faturas_vazio(self, client):
        resp = client.get("/faturas")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_listar_e_get(self, client):
        # Upload uma fatura
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        fatura_id = resp.json()["id"]

        # GET /faturas tem 1 item
        resp = client.get("/faturas")
        assert resp.status_code == 200
        faturas = resp.json()
        assert len(faturas) == 1
        assert faturas[0]["id"] == fatura_id
        assert faturas[0]["banco"] == "sofisa"
        assert faturas[0]["fechamento"] == "2026-09-15"
        assert faturas[0]["total_a_pagar"] == "6727.55"

        # GET /faturas/{id} retorna payload completo
        resp = client.get(f"/faturas/{fatura_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fatura_id
        assert data["banco"] == "sofisa"
        assert "transacoes" in data
        assert len(data["transacoes"]) == 96

    def test_get_fatura_id_inexistente(self, client):
        resp = client.get("/faturas/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestDelete:

    def test_delete_ok(self, client):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        fatura_id = resp.json()["id"]

        # DELETE a fatura
        resp = client.delete(f"/faturas/{fatura_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["id"] == fatura_id

        # PDF removido do disco
        pdf_path = api.UPLOADS_DIR / f"{fatura_id}.pdf"
        assert not pdf_path.exists(), "PDF nao foi removido do disco"

        # GET /faturas/{id} -> 404
        resp = client.get(f"/faturas/{fatura_id}")
        assert resp.status_code == 404

        # GET /faturas -> lista vazia
        resp = client.get("/faturas")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_delete_inexistente(self, client):
        resp = client.delete("/faturas/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestOverrideNaLeitura:
    """Override manual vale IMEDIATAMENTE no GET (sem POST /recategorizar)."""

    # 'Compra a Vista NONO CAFE' aparece 9x na fatura sofisa com a mesma
    # descricao -> o override por descricao normalizada muda as 9 de uma vez.
    DESCRICAO = "Compra a Vista NONO CAFE"
    CATEGORIA_REGRA = "Restaurante/Cafe"
    CATEGORIA_OVERRIDE = "Teste"

    def _upload(self, client) -> dict:
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert resp.status_code == 200
        return resp.json()

    def _cats_no_get(self, client, fid) -> list[str]:
        transacoes = client.get(f"/faturas/{fid}").json()["transacoes"]
        return [t["categoria"] for t in transacoes if t["descricao"] == self.DESCRICAO]

    def _put_override(self, client, categoria: str):
        from urllib.parse import quote
        return client.put(
            f"/overrides/{quote(self.DESCRICAO, safe='*')}",
            json={"categoria": categoria},
        )

    def test_override_reflete_no_get_sem_recategorizar(self, client):
        data = self._upload(client)
        fid = data["id"]

        # estado inicial: categoria vem da REGRA (parse)
        antes = self._cats_no_get(client, fid)
        assert len(antes) == 9
        assert set(antes) == {self.CATEGORIA_REGRA}

        # PUT do override — sem chamar /recategorizar em momento algum
        resp = self._put_override(client, self.CATEGORIA_OVERRIDE)
        assert resp.status_code == 200
        assert resp.json()["chave"] == self.DESCRICAO.upper()

        depois = self._cats_no_get(client, fid)
        assert len(depois) == 9, "todas as linhas com a mesma descricao devem mudar"
        assert set(depois) == {self.CATEGORIA_OVERRIDE}

        # o JSONL continua com o resultado do PARSE (override e aplicado na leitura)
        reg = json.loads(api.JSONL_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
        no_disco = [t["categoria"] for t in reg["payload"]["transacoes"] if t["descricao"] == self.DESCRICAO]
        assert set(no_disco) == {self.CATEGORIA_REGRA}

    def test_delete_override_volta_a_categoria_original(self, client):
        data = self._upload(client)
        fid = data["id"]
        assert self._put_override(client, self.CATEGORIA_OVERRIDE).status_code == 200
        assert set(self._cats_no_get(client, fid)) == {self.CATEGORIA_OVERRIDE}

        from urllib.parse import quote
        resp = client.delete(f"/overrides/{quote(self.DESCRICAO, safe='*')}")
        assert resp.status_code == 200

        assert set(self._cats_no_get(client, fid)) == {self.CATEGORIA_REGRA}

    def test_upload_response_reflete_override_existente(self, client):
        """A resposta do POST /faturas também passa pelos overrides."""
        self._upload(client)
        self._put_override(client, self.CATEGORIA_OVERRIDE)

        data = self._upload(client)
        cats = [t["categoria"] for t in data["transacoes"] if t["descricao"] == self.DESCRICAO]
        assert cats and set(cats) == {self.CATEGORIA_OVERRIDE}

    def test_sem_override_get_inalterado(self, client):
        """Sem overrides, o GET é idêntico ao payload persistido."""
        data = self._upload(client)
        fid = data["id"]
        assert set(self._cats_no_get(client, fid)) == {self.CATEGORIA_REGRA}
        assert client.get("/overrides").json() == {}


class TestPersistencia:

    def test_jsonl_persistido(self, client):
        with open(FATURA_PDF, "rb") as f:
            client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert api.JSONL_PATH.exists()
        lines = api.JSONL_PATH.read_text().strip().splitlines()
        assert len(lines) == 1
        reg = json.loads(lines[0])
        assert "id" in reg
        assert "criado_em" in reg
        assert "payload" in reg
        assert reg["payload"]["banco"] == "sofisa"
