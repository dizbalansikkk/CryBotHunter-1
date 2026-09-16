FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-rl.txt ./
RUN pip install --no-cache-dir -r requirements-rl.txt
# Fail the image build early if the RL runtime dependency was not installed.
RUN python -c "from stable_baselines3 import PPO"

COPY . .

CMD ["sh", "entrypoint.sh"]
