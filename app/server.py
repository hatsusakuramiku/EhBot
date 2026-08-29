import uvicorn

from app.config import Settings
from app.logging import configure_logging


def main() -> None:
    settings = Settings.from_env()
    # Before `uvicorn.run`, and with `log_config=None` below, so that uvicorn
    # leaves the configuration alone. Its default `LOGGING_CONFIG` attaches
    # handlers to `uvicorn` and `uvicorn.access` and turns propagation off,
    # which would restore the plain-text access log -- the one output path that
    # bypasses redaction.
    configure_logging(
        level=settings.log_level,
        access_log=settings.log_access,
        log_dir=settings.log_dir if settings.log_to_file else None,
        file_max_bytes=settings.log_file_max_bytes,
        file_backups=settings.log_file_backups,
        force=True,
    )
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        workers=1,
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips=",".join(settings.trusted_proxy_ips),
        log_config=None,
    )


if __name__ == "__main__":
    main()
