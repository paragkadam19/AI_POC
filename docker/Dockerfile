FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fix SSL certificate issue
ENV PYTHONHTTPSVERIFY=0
ENV AWS_CA_BUNDLE=""

CMD ["python", "run_all.py"]
