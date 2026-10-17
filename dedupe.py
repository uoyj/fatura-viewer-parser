from hashlib import sha256


def fingerprint_pdf(conteudo: bytes) -> str:
    return sha256(conteudo).hexdigest()


def chave_natural(payload: dict) -> tuple:
    """Rede de segurança para PDFs re-emitidos (hash diferente, mesmo conteúdo)."""
    return (
        payload["banco"],
        payload["fechamento"],
        payload["total_a_pagar"],      # já é string, comparação exata
        len(payload["transacoes"]),
    )


def encontrar_duplicata(fp: str, chave: tuple,
                        registros: list[dict]) -> dict | None:
    for reg in registros:
        if reg.get("pdf_hash") == fp:          # match forte
            return reg
        if chave_natural(reg["payload"]) == chave:  # match fraco
            return reg
    return None
