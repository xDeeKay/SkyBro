FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests ephem
RUN groupadd -g 1000 skybro && useradd -u 1000 -g skybro -m skybro
COPY tracker.py .
COPY weather.py .
COPY astronomy.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "tracker.py"]
