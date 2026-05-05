FROM golang:1.26.2-alpine  AS builder
WORKDIR /app
COPY /app /app/
RUN go mod download && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w" -o app

FROM alpine:3.22.4
RUN apk update && apk add --no-cache curl=8.14.1-r2
WORKDIR /app
COPY --from=builder /app/app /app/
RUN adduser -D appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fs httt://localhost:8080/healthz
ENTRYPOINT ["./app"]
