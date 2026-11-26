# Стенд: приложение + корпус регламентов. Состояние (база, индекс, база знаний) — в томе REGQA_STATE.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 REGQA_STATE=/var/lib/iikuznica
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY static static
COPY data data
RUN mkdir -p /var/lib/iikuznica && useradd --uid 10001 --create-home stand \
    && chown -R stand:stand /var/lib/iikuznica
USER stand

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
