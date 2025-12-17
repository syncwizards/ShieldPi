# Base: Python 3.11 Slim
FROM python:3.11-slim

LABEL maintainer="SyncWizards"
LABEL project="ShieldPi Core"

ENV DEBIAN_FRONTEND=noninteractive

# 1. Instalar herramientas del sistema y TZDATA (Importante para que funcione ZoneInfo)
# NOTA: No seteamos la zona horaria aquí, dejamos que el usuario lo haga en el Stack.
RUN apt-get update && apt-get install -y \
    curl \
    docker.io \
    cron \
    gcc \
    python3-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 2. Instalar Kopia
ARG KOPIA_VERSION=0.15.0
RUN curl -L https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-arm64.tar.gz | tar xz \
    && mv kopia-${KOPIA_VERSION}-linux-arm64/kopia /usr/local/bin/kopia \
    && rm -rf kopia-${KOPIA_VERSION}-linux-arm64 \
    && chmod +x /usr/local/bin/kopia

# 3. Entorno Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiar código
COPY app /app

# 5. Variables
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# 6. Inicio
CMD ["flask", "run", "--port=51515"]
