FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests ephem apprise
RUN groupadd -g 1000 skybro && useradd -u 1000 -g skybro -m skybro
COPY tracker.py .
COPY weather.py .
COPY astronomy.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "tracker.py"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import time,sys; sys.exit(0 if time.time()-float(open('/tmp/heartbeat').read())<300 else 1)"
