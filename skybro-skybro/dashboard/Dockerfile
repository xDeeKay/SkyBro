FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests gunicorn
COPY . .
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "app:app"]
