# Automated PoC diagnostics

This directory is maintained automatically by the Home Assistant **Reolink Battery RTSP PoC** integration.

- `latest.json` is replaced after each manual Live View probe when GitHub diagnostics upload is enabled.
- Git history preserves previous probe results.
- The uploader intentionally excludes GitHub tokens, camera credentials, UID, IP addresses and raw media/protocol payloads.
- This branch is not used by HACS for installation.
