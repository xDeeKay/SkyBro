FROM python:3.12-slim
WORKDIR /app
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}
ENV PORT=5000
RUN pip install --no-cache-dir flask requests gunicorn apprise
RUN groupadd -g 1000 skybro && useradd -u 1000 -g skybro -m skybro
COPY . .
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import os,requests,sys; r=requests.get('http://localhost:'+os.environ.get('PORT','5000')+'/api/health',timeout=3); sys.exit(0 if r.ok else 1)"
