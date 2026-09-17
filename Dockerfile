FROM python:3.12-slim

# Instalar curl (para HEALTHCHECK) e deps do sistema
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Criar diretorio da aplicacao
WORKDIR /app

# Copiar lockfile/pyproject primeiro (cache de dependencias)
COPY pyproject.toml ./

# Instalar dependencias (sem grupo dev)
# pdfplumber+pymupdf exigem compilacao de C — instalar build deps via pip
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "python-multipart" "httpx" "pdfplumber>=0.11.0" "pymupdf>=1.28.0"

# Copiar codigo da aplicacao (nao data/ nem tests/ — via .dockerignore)
COPY extractors/ extractors/
COPY parsers/ parsers/
COPY registry.py .
COPY schemas.py .
COPY categorizer.py .
COPY api.py .
COPY static/ static/

# Criar pasta data (volume) e usuario nao-root
RUN mkdir -p data/uploads && \
    useradd -r -s /bin/false app && \
    chown -R app:app /app

# Porta
EXPOSE 8000

# Volume para uploads + JSONL (persistencia)
VOLUME ["/app/data"]

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/parsers || exit 1

# Rodar como usuario app
USER app

# Server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
