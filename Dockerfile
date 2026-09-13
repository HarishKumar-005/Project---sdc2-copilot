# ==============================================================================
# SCD2 Copilot — Container Specification
# Python 3.12 Slim Linux Container for Streamlit & In-Process SCD2 Engine
# ==============================================================================

FROM python:3.12-slim

# Set environment variables for clean, unbuffered container execution
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/src:/app/app \
    PREFECT_SERVER_ANALYTICS_ENABLED=false

# Create non-root system user and group
RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --create-home --no-log-init --shell /bin/bash appuser

WORKDIR /app

# Install dependencies in an isolated cached layer
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application source, sample data, and configuration
COPY --chown=appuser:appgroup . /app

# Ensure runtime mutable directories exist with non-root ownership across /app/data
RUN mkdir -p /app/data/runs/.fingerprints /app/data/quarantine /app/data/staging && \
    chown appuser:appgroup /app && \
    chown -R appuser:appgroup /app/data && \
    chmod -R 775 /app/data

# Switch to non-root user
USER appuser

# Expose default Streamlit port
EXPOSE 8501

# Standard Streamlit health check via built-in urllib (no extra packages needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health').read()" || exit 1

# Production startup: Streamlit in foreground
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
