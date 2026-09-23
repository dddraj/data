"""Launch the Facebook Ads MCP server from any working directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fb_ads.server import main  # noqa: E402

main()
