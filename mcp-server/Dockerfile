FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy server code and requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project so configs and data dirs are accessible
COPY .. .

# Run the server from the mcp-server directory
WORKDIR /app/mcp-server
CMD ["uvicorn", "qna_action_mcp_server:app", "--host", "0.0.0.0", "--port", "8000"]
