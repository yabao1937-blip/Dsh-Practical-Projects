"""OPC UA 只读适配器：不浏览/写节点，不发现后自动降低安全级别。"""
import asyncio
import logging
import os

from .domain import normalize, utc_now


class ConfigurationError(RuntimeError):
    pass


async def configure_security(client, cfg):
    from asyncua import ua
    from asyncua.crypto import uacrypto
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
    from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
    from asyncua.common.utils import ServiceError
    try:
        certificate = await uacrypto.load_certificate(cfg.client_certificate)
        description = ua.ApplicationDescription(ApplicationUri=cfg.application_uri, ApplicationType=ua.ApplicationType.Client)
        await CertificateValidator(CertificateValidatorOptions.BASIC_VALIDATION)(certificate, description)
    except (ServiceError, ValueError, OSError) as exc:
        raise ConfigurationError("client certificate invalid") from exc
    client.certificate_validator = CertificateValidator(
        CertificateValidatorOptions.BASIC_VALIDATION | CertificateValidatorOptions.PEER_SERVER)
    await client.set_security(SecurityPolicyBasic256Sha256, cfg.client_certificate, cfg.client_key,
        server_certificate=cfg.server_certificate, mode=ua.MessageSecurityMode.SignAndEncrypt)


class OpcUaReader:
    def __init__(self, cfg):
        self.cfg, self.client = cfg, None

    async def __aenter__(self):
        from asyncua import Client
        logging.getLogger("asyncua").setLevel(logging.CRITICAL)
        cfg = self.cfg
        self.client = Client(cfg.endpoint, timeout=cfg.timeout_seconds)
        self.client.application_uri = cfg.application_uri
        if cfg.username_env:
            username, password = os.environ.get(cfg.username_env), os.environ.get(cfg.password_env)
            if not username or not password:
                raise ConfigurationError("credentials environment missing")
            self.client.set_user(username)
            self.client.set_password(password)
        try:
            async with asyncio.timeout(cfg.timeout_seconds * 3):
                if cfg.security != "None":
                    await configure_security(self.client, cfg)
                await self.client.connect()
                self.nodes = [self.client.get_node(point.node_id) for point in cfg.points]
            return self
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        if self.client:
            try:
                async with asyncio.timeout(self.cfg.timeout_seconds):
                    await self.client.disconnect()
            except Exception:
                pass

    async def __aexit__(self, *_args):
        await self._close()

    async def read(self):
        from asyncua import ua
        samples = []
        async with asyncio.timeout(self.cfg.timeout_seconds * 2):
            for offset in range(0, len(self.nodes), 100):
                parameters = ua.ReadParameters()
                parameters.TimestampsToReturn = ua.TimestampsToReturn.Both
                parameters.MaxAge = 0
                parameters.NodesToRead = [ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value)
                    for node in self.nodes[offset:offset + 100]]
                values = await self.client.uaclient.read(parameters)
                received = utc_now()
                samples.extend(normalize(point, value.Value.Value if value.Value else None,
                    int(value.StatusCode.value), value.SourceTimestamp, value.ServerTimestamp, received)
                    for point, value in zip(self.cfg.points[offset:offset + 100], values, strict=True))
        return samples
