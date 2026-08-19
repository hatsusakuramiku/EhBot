import uvicorn

from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        workers=1,
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips=",".join(settings.trusted_proxy_ips),
    )


if __name__ == "__main__":
    main()
