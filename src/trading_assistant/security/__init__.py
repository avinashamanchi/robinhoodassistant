"""Security primitives that do not load secrets implicitly."""

from .secrets import RuntimeSecrets, SecretProvider

__all__ = ["RuntimeSecrets", "SecretProvider"]
