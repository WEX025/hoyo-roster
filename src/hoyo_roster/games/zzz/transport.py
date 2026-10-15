"""Read registered ZZZ source records from the attached client browser."""

from .endpoint_specs import require_registered_endpoint
from .errors import ConfigurationError, ApiResponseError
from hoyo_roster.source import ClientSource


class ClientTransport(ClientSource):
    def __init__(self, session):
        super().__init__(session, "zzz")

    async def request_json(self, endpoint, *, params=None, body=None):
        spec = require_registered_endpoint(endpoint)
        if body is not None and not spec.allows_body:
            raise ConfigurationError("Source does not accept a body")
        if spec.method == "POST" and body is None:
            raise ConfigurationError("Source requires a body")
        data, _ = await self.read(spec, params=params, body=body)
        if any(field not in data for field in spec.required_response_fields):
            raise ApiResponseError("Source response fields missing")
        return data
