import pydantic

from app.services.errors import ConfigurationNotFound
from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions, UIOptions
from .core import (
    AuthActionConfiguration,
    ExecutableActionMixin,
    InternalActionConfiguration,
    PullActionConfiguration,
)


class CredentialsConfig(AuthActionConfiguration, ExecutableActionMixin):
    username: str = FieldWithUIOptions(
        ...,
        title="Username",
        description="Username for the Savannah Tracking API.",
    )
    password: pydantic.SecretStr = FieldWithUIOptions(
        ...,
        format="password",
        title="Password",
        description="Password for the Savannah Tracking API.",
        ui_options=UIOptions(
            widget="password",
        ),
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["username", "password"],
    )


DEFAULT_SUBJECT_TYPE = "unassigned"


class ReadObservationsConfig(PullActionConfiguration):
    lookback_days: int = FieldWithUIOptions(
        3,
        ge=1,
        le=30,
        title="Data lookback days",
        description="Number of days to look back for data. Older records are discarded, "
                    "except the newest one which is kept to reflect the collar's last known position.",
        ui_options=UIOptions(
            widget="range",
        ),
    )
    subject_type: str = FieldWithUIOptions(
        DEFAULT_SUBJECT_TYPE,
        title="Subject type",
        description="Subject type to assign to the observations (e.g. elephant, giraffe, ranger).",
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["lookback_days", "subject_type"],
    )


class ReadObservationsPerCollarConfig(InternalActionConfiguration):
    collar_id: str
    lookback_days: int = 3
    subject_type: str = DEFAULT_SUBJECT_TYPE


def get_auth_config(integration) -> CredentialsConfig:
    auth_config = integration.get_action_config("check_credentials")
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} are missing. "
            f"Please fix the integration setup in the portal."
        )
    return CredentialsConfig.parse_obj(auth_config.data)
