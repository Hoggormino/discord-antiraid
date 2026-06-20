# Host-agnostic image for the anti-raid bot (works on Railway, Fly.io, Render,
# or any Docker host / VPS). The bot reads DISCORD_TOKEN from the environment —
# set it as a secret on the host, never bake it into the image.
FROM python:3.12-slim

# Don't write .pyc files; flush logs immediately so host log viewers see them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY antiraid/ ./antiraid/
COPY run.py .

# Long-running worker (no exposed port — it's a gateway client, not a web app).
CMD ["python", "run.py"]
