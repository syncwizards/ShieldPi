# Base: Python 3.11 Slim (Ligero y moderno)
FROM python:3.11-slim

# Metadatos
LABEL maintainer="SyncWizards"
LABEL project="ShieldPi Core"

# 1. Instalar herramientas del sistema, Cliente Docker y COMPILADORES
# Agregamos 'gcc' y 'python3-dev' para poder compilar psutil
RUN apt-get update && apt-get install -y \
    curl \
    docker.io \
    cron \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Instalar el Motor Kopia (Solo CLI)
ARG KOPIA_VERSION=0.15.0
RUN curl -L https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-arm64.tar.gz | tar xz \
    && mv kopia-${KOPIA_VERSION}-linux-arm64/kopia /usr/local/bin/kopia \
    && rm -rf kopia-${KOPIA_VERSION}-linux-arm64 \
    && chmod +x /usr/local/bin/kopia

# 3. Configurar entorno Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiar nuestro codigo fuente
COPY app /app

# 5. Variables de entorno
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# 6. Comando de inicio
CMD ["flask", "run", "--port=51515"]
