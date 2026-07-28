"""OpenAI-compatible provider with no concrete HTTP/SDK dependency."""

from .provider import (
    AuthenticationError,
    InvalidResponseError,
    InvalidRequestError,
    OpenAICompatibleModel,
    ProviderError,
    RateLimitError,
    RetryableProviderError,
    TransportError,
)

__all__ = [
    "AuthenticationError",
    "InvalidResponseError",
    "InvalidRequestError",
    "OpenAICompatibleModel",
    "ProviderError",
    "RateLimitError",
    "RetryableProviderError",
    "TransportError",
]
