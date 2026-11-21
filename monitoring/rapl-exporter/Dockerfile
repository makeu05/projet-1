FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .
EXPOSE 9106
CMD ["python3", "-u", "exporter.py"]
