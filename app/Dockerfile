FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.1.1 paho-mqtt==2.1.0 waitress==3.0.2
COPY bridge.py server.py ./
COPY templates ./templates
ENV ROOMBA_DATA=/app/data
EXPOSE 8080
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "--threads=4", "server:app"]
