FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY *.py .
COPY agent_input.csv .

# Create output directory
RUN mkdir -p output

# Default: run campaign in dry-run mode
CMD ["python", "-m", "waa", "campaign"]
