# Xray VMess service

The production Xray configuration is stored at
`.secrets/xray/config.json`. The `.secrets/` directory is ignored by Git so
the VMess client UUID is never committed.

For a new deployment:

1. Copy `docker/xray/config.example.json` to `.secrets/xray/config.json`.
2. Replace `REPLACE_WITH_EXISTING_VMESS_UUID` with the client UUID.
3. Restrict the file permissions with `chmod 600 .secrets/xray/config.json`.
4. Validate the configuration before deployment:

   ```bash
   docker run --rm \
     -v "$PWD/.secrets/xray/config.json:/etc/xray/config.json:ro" \
     ghcr.io/xtls/xray-core:1.8.24 \
     run -test -config /etc/xray/config.json
   ```

5. Start Xray and recreate Caddy so it resolves the `xray` service on the
   shared Docker network:

   ```bash
   docker compose -f docker-compose.monitoring.yml up -d xray caddy
   ```

Port `52799` is intentionally not published on the host. Caddy is the only
public entrypoint and proxies VMess WebSocket traffic to `xray:52799` over the
external `edge-shared` network.
