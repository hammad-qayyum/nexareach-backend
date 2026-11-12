"""Error tracking (Sentry, or self-hosted GlitchTip which speaks the same protocol)."""

import sentry_sdk

from app.core.config import get_settings


def init_error_tracking(component: str) -> None:
    dsn = get_settings().sentry_dsn
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        # Never send request bodies, cookies or user details: they hold leads' and users' personal data.
        send_default_pii=False,
        traces_sample_rate=0,
        server_name=component,
    )
