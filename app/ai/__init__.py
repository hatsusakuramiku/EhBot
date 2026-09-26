"""AI path providers: an OpenAI-compatible chat client and its settings.

This package owns *how* to talk to a model -- the provider list, the API keys
that rotate behind them, the primary/fallback chain and the connectivity
verification the settings page insists on. It does not own *what* the model is
asked (that prompt lives in the archive settings) and it does not decide where a
book goes (that is `ConversionService`, which reads this service's answer).

The split is deliberate: everything here is testable with a fake HTTP client and
no metadata, and the packing path stays unaware of which vendor answered.
"""

from app.ai.errors import AiError

__all__ = ["AiError"]
