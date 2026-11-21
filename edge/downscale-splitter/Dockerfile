FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY worker.py .
CMD ["python3", "-u", "worker.py"]
