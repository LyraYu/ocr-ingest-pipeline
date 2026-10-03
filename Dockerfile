FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# CPU-only torch (the default wheel bundles CUDA, ~2 GB larger).
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the default embedding model into the image so `docker compose up` needs no
# network at run time. Other models (e.g. BAAI/bge-small-en-v1.5) download on demand.
COPY app/embedding.py /tmp/embedding.py
RUN python -c "import sys; sys.path.insert(0, '/tmp'); from embedding import resolve_model; \
print(resolve_model('sentence-transformers/all-MiniLM-L6-v2'))"

COPY . .

# Code version recorded on every pipeline_runs row: the CODE_VERSION build arg when
# given, else the short sha read from .git/HEAD, else "unknown" (see app/version.py).
ARG CODE_VERSION=
ENV CODE_VERSION=${CODE_VERSION}
RUN python -m app.version "${CODE_VERSION}" > CODE_VERSION && echo "code version: $(cat CODE_VERSION)"

EXPOSE 8000
CMD ["sh", "-c", "python -m app.cli migrate && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
