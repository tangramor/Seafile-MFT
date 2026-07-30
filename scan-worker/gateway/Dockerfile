FROM python:3.12-slim

RUN pip install flask minio kafka-python

WORKDIR /app
COPY app.py /app/

RUN mkdir -p /tmp/uploads

EXPOSE 8080

CMD ["python", "-u", "app.py"]
