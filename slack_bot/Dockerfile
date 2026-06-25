# DBAOps Slack 봇 — Socket Mode. agentcore_client 는 UI 와 공유.
FROM python:3.12-slim

WORKDIR /app

COPY slack_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 봇 코드 + UI 의 agentcore_client(invoke_stream) 재사용
COPY slack_bot/bot.py slack_bot/render.py /app/
COPY ui/streamlit/agentcore_client.py /app/agentcore_client.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
