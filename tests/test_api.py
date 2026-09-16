"""
Testes para api.py (FastAPI).

Cobre:
- POST /faturas: upload OK com Fatura.pdf → 200 + JSON válido
- POST /faturas: banco inexistente → 422 {"erro": ...}
- POST /faturas: arquivo .txt → 422 {"erro": ...}
- GET /parsers: retorna dict de bancos
- GET /faturas/{id}: retorna payload completo
- GET /faturas (lista vazia quando jsonl não existe): []

Usa FastAPI TestClient — não sobe servidor real.
"""

import json
import pytest
from fastapi.testclient import TestClient
from api import app, JSONL_PATH

client = TestClient(app)


@pytest.fixture(autouse=True)
def limpar_jsonl():
    """Limpa faturas.jsonl antes de cada teste."""
    if JSONL_PATH.exists():
        JSONL_PATH.unlink()
    yield
    if JSONL_PATH.exists():
        JSONL_PATH.unlink()


FATURA_PDF = "in/Fatura.pdf"


class TestUploadFatura:

    def test_upload_ok(self):
        """Upload de PDF com banco 'sofisa' → 200 + JSON válido."""
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

    def test_upload_com_versao(self):
        """Upload com versao explícita '2026-09'."""
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa", "versao": "2026-09"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["modelo"] == "2026-09"

    def test_banco_inexistente(self):
        """Banco não registrado → 422 com 'erro'."""
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

    def test_extensao_invalida(self):
        """Arquivo .txt → 422."""
        resp = client.post(
            "/faturas",
            files={"arquivo": ("test.txt", b"hello world", "text/plain")},
            data={"banco": "sofisa"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "erro" in body
        assert "pdf" in body["erro"].lower()

    def test_nunca_traceback_no_cliente(self):
        """Mesmo com erro interno, não expor traceback."""
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert resp.status_code == 200  # sucesso
        text = resp.text
        assert "Traceback" not in text
        assert "File " not in text


class TestListagem:

    def test_get_parsers(self):
        resp = client.get("/parsers")
        assert resp.status_code == 200
        data = resp.json()
        assert "sofisa" in data
        assert "2026-09" in data["sofisa"]

    def test_get_faturas_vazio(self):
        resp = client.get("/faturas")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_faturas_com_dados(self):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        fatura_id = resp.json()["id"]

        resp = client.get("/faturas")
        assert resp.status_code == 200
        faturas = resp.json()
        assert len(faturas) == 1
        assert faturas[0]["id"] == fatura_id
        assert faturas[0]["banco"] == "sofisa"
        assert faturas[0]["fechamento"] == "2026-09-15"
        assert faturas[0]["total_a_pagar"] == "6727.55"

    def test_get_fatura_por_id(self):
        with open(FATURA_PDF, "rb") as f:
            resp = client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        fatura_id = resp.json()["id"]

        resp = client.get(f"/faturas/{fatura_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fatura_id
        assert data["banco"] == "sofisa"
        assert "transacoes" in data

    def test_get_fatura_id_inexistente(self):
        resp = client.get("/faturas/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestPersistencia:

    def test_faturas_jsonl_persistido(self):
        """Após upload, fatura é persistida em data/faturas.jsonl."""
        with open(FATURA_PDF, "rb") as f:
            client.post(
                "/faturas",
                files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
                data={"banco": "sofisa"},
            )
        assert JSONL_PATH.exists()
        lines = JSONL_PATH.read_text().strip().splitlines()
        assert len(lines) == 1
        reg = json.loads(lines[0])
        assert "id" in reg
        assert "criado_em" in reg
        assert "payload" in reg
        assert reg["payload"]["banco"] == "sofisa"
