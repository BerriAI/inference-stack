FROM alpine/helm:4.3.2@sha256:a3b9d617c0c30a6c488cebb429c4ba83995a26dc2184599af5fd408a361b2e67 AS helm
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
