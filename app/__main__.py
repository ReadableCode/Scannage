"""Starts the app: python -m app.

Plain http unless SCANNAGE_HTTPS is on. With it on, the same port speaks
https only, using a certificate this machine made for itself.
"""

import logging

import uvicorn

from . import config, tls

log = logging.getLogger("scannage")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
    options = {}
    if config.HTTPS:
        cert_path, key_path, fingerprint = tls.ensure_certificate(config.TLS_DIR, config.HTTPS_HOSTS)
        log.info("https is on, certificate %s", cert_path)
        log.info("certificate SHA-256 fingerprint: %s", fingerprint)
        options = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT, **options)


if __name__ == "__main__":
    main()
