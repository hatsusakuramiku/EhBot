from __future__ import annotations


class ArchiveError(ValueError):
    """Recoverable archive-processing failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ArchiveSafetyError(ArchiveError):
    """The archive violated a pre-extraction safety limit."""


class ArchivePasswordRequired(ArchiveError):
    """No known password opened the archive."""

    def __init__(
        self,
        message: str = "\u5f52\u6863\u5df2\u52a0\u5bc6\uff0c\u5f53\u524d\u5bc6\u7801\u5e93\u65e0\u6cd5\u6253\u5f00",
    ) -> None:
        super().__init__("ARCHIVE_PASSWORD_REQUIRED", message)


class ArchiveVolumesMissing(ArchiveError):
    """A split archive is incomplete."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        listed = "\u3001".join(missing)
        super().__init__(
            "ARCHIVE_VOLUMES_MISSING",
            f"\u5206\u5377\u5f52\u6863\u7f3a\u5c11\u5206\u5377\uff1a{listed}",
        )
        self.missing = missing


class ArchiveToolUnavailable(ArchiveError):
    """The registered tool profile cannot be used on this host."""

    def __init__(self, message: str) -> None:
        super().__init__("ARCHIVE_TOOL_UNAVAILABLE", message)


class ArchiveBackendUnsupported(ArchiveError):
    """This backend cannot read the archive, but another profile might.

    It is raised while *admitting* an archive -- listing it, or resolving its
    password -- and is caught by `ArchiveProcessor` to move on to the next
    eligible profile. When no profile accepts the archive it surfaces as a
    plain `ArchiveError` with `ARCHIVE_COMPRESSION_UNSUPPORTED`: the difference
    between "this backend can't" and "nothing here can" is the fallback.
    The code is deliberately reused so the operator-facing vocabulary does not
    grow a word for an internal retry.
    """

    def __init__(
        self,
        message: str = (
            "\u538b\u7f29\u5305\u4f7f\u7528\u4e86\u5185\u7f6e\u89e3\u538b\u5668"
            "\u4e0d\u652f\u6301\u7684\u52a0\u5bc6\u6216\u538b\u7f29\u65b9\u5f0f"
        ),
    ) -> None:
        super().__init__("ARCHIVE_COMPRESSION_UNSUPPORTED", message)


class UnsupportedArchiveFormat(ArchiveError):
    def __init__(self, source_format: str) -> None:
        super().__init__(
            "UNSUPPORTED_FORMAT",
            f"\u538b\u7f29\u683c\u5f0f {source_format} \u6682\u4e0d\u652f\u6301",
        )
        self.source_format = source_format


__all__ = [
    "ArchiveBackendUnsupported",
    "ArchiveError",
    "ArchivePasswordRequired",
    "ArchiveSafetyError",
    "ArchiveToolUnavailable",
    "ArchiveVolumesMissing",
    "UnsupportedArchiveFormat",
]
