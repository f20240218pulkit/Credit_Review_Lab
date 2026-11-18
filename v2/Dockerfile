FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home reviewer && mkdir -p /app/data && chown reviewer /app/data
COPY credit_review ./credit_review
USER reviewer
ENV CREDIT_DB=/app/data/credit-review.sqlite3 CREDIT_PUBLIC_DEMO=1
EXPOSE 8000
CMD ["uvicorn", "credit_review.api:app", "--host", "0.0.0.0", "--port", "8000"]
