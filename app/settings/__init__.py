"""System preferences.

`app.settings.service` is intentionally not re-exported here: it depends on
`app.db.database`, the same reason `app.archive.service` is absent from
`app.archive`'s exports.
"""

__all__: list[str] = []
