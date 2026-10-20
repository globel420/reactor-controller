# Match the supervisor's immutable multi-architecture Python base so the
# simulation profile is reproducible on both x86_64 and Raspberry Pi arm64.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

# Copy emulator scripts
COPY emulators/ .

# The command will be overridden in docker-compose.yml for each service
