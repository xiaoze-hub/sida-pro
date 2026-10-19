FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . ./tdx_gateway/
COPY http_schema.json /app/contract/http_schema.json

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=8s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5)" || exit 1

CMD ["uvicorn", "tdx_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
