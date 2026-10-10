"""Staged Hermes Chat Completions provider using only a per-turn AF_UNIX transport.

No upstream credentials or native subscription clients enter the sandbox. The
host InferenceCapability remains responsible for destination, quota and auth.
"""
import os
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

NAME = 'diaktoros-seatbelt-wire'


class UnixWireProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        import httpx
        from openai import OpenAI

        endpoint = Path(os.environ['DIAKTOROS_INFERENCE_SOCKET'])
        if not endpoint.is_absolute() or not endpoint.is_socket():
            raise RuntimeError('live inference socket required')
        # Ignore caller-selected URLs, proxy settings and authentication. The dummy
        # header is discarded by the credential-owning host capability.
        transport = httpx.HTTPTransport(uds=str(endpoint), retries=0)
        client = httpx.Client(transport=transport, trust_env=False,
                              follow_redirects=False, timeout=135)
        try:
            return OpenAI(base_url='http://localhost/v1', api_key='sandbox-dummy',
                          http_client=client, max_retries=0)
        except BaseException:
            client.close()
            raise


register_provider(UnixWireProfile(
    name=NAME, display_name='Diaktoros native Unix wire', api_mode='chat_completions',
    auth_type='api_key', env_vars=('OPENAI_API_KEY',),
    base_url='http://localhost/v1', supports_model_listing=False,
    supports_health_check=False,
))
