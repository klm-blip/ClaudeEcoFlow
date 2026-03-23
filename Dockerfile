FROM python:3.13-slim

WORKDIR /app

# Install dependencies first (cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application package + arbiter (used by simulator)
COPY ecoflow_web/ ecoflow_web/
COPY arbiter/ arbiter/

EXPOSE 5000

CMD ["python", "-m", "ecoflow_web"]
