FROM python:3.11-slim

WORKDIR /app

# fonts-dejavu-core is used for scores/numbers. The team-name font (PT Sans
# Narrow) is bundled directly under assets/fonts, no OS package needed.
# curl is required by sofascore.py - Sofascore's API blocks Python's requests
# library via TLS fingerprinting but not curl itself (confirmed live).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
