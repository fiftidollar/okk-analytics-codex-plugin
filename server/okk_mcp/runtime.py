"""Shared outbound OKK client used by OAuth verification and analytics tools."""

from okk_mcp.config import get_settings
from okk_mcp.platform_client import OKKPlatformClient

platform_client = OKKPlatformClient(get_settings())
