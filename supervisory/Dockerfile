# Keep the human-readable patch tag and the multi-architecture manifest digest
# together. This digest includes linux/arm64 for Raspberry Pi OS 64-bit.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

# Copy requirements and install dependencies
COPY supervisory/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the supervisory application files
COPY supervisory/ ./supervisory/
COPY scripts/load_firmware_env.py ./scripts/load_firmware_env.py

# Expose port 8000 for FastAPI
EXPOSE 8000

# Start the application
CMD ["uvicorn", "supervisory.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
