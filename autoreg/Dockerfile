FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.49.0 fastapi uvicorn requests
COPY server.py /app/server.py
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8082"]
