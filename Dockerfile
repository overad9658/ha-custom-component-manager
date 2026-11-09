ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace/src:/workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    openssh-server \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt pyproject.toml setup.py README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip install --no-build-isolation -e .

COPY docker ./docker
COPY tests ./tests
COPY components-example.yaml ./

RUN chmod +x /workspace/docker/ha_instance_entrypoint.sh

CMD ["pytest", "-v"]
