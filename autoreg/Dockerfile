FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.49.0 fastapi uvicorn requests
ENV DISPLAY=:99
COPY server.py /app/server.py
CMD ["sh", "-c", "rm -f /tmp/.X99-lock && Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -ac & sleep 1 && exec uvicorn server:app --host 0.0.0.0 --port 8082"]
