FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest.py .

# session.session, .env, telegraph_token.txt, and html/ are mounted at runtime
# see docker-compose.yml or the run command in README

CMD ["python", "digest.py"]
