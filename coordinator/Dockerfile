FROM python:3.11-slim

WORKDIR /app

# Copy project structure:
#   /app/
#     common/      <- common package
#     coordinator/ <- coordinator package (server + config)
COPY common /app/common
COPY coordinator /app/coordinator

# Fix the Python path so 'coordinator' module can import 'common'
ENV PYTHONPATH=/app

RUN pip install --no-cache-dir flask pika requests

EXPOSE 5000

CMD ["python", "coordinator/coordinator_server.py"]
