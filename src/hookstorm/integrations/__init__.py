"""Optional framework adapters and shared webhook test-recording utilities.

Importing this package never imports FastAPI or Flask. Install the matching
extra before using its adapter module.
"""

from hookstorm.integrations._recorder import (
    WebhookRecord,
    WebhookRecorder,
    WebhookRequestError,
)

__all__ = ["WebhookRecord", "WebhookRecorder", "WebhookRequestError"]
