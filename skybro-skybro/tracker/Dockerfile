FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests ephem
COPY tracker.py .
COPY weather.py .
COPY astronomy.py .
CMD ["python", "-u", "tracker.py"]
