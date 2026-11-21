FROM alpine/helm:3.17.3 AS helm
FROM python:3.12-slim
COPY --from=helm /usr/bin/helm /usr/local/bin/helm
COPY charts/model /opt/charts/model
RUN helm dependency build /opt/charts/model
WORKDIR /app
COPY launcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY launcher/ .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
