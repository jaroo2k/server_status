FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
ARG DEV=0
RUN pip install --no-cache-dir -r $([ "$DEV" = "1" ] && echo requirements-dev.txt || echo requirements.txt)
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
