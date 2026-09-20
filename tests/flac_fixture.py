"""10ms mono silence encoded by FFmpeg, with no metadata padding."""

import base64

SILENT_FLAC = base64.b64decode(
    "ZkxhQwAAACIAUABQAAAAAAC1AfQA8AAAAAAAAAAAAAAAAAAAAAAAAAAAhAAALAwAAABM"
    "YXZmNjMuMS4xMDEBAAAAFAAAAGVuY29kZXI9TGF2ZjYzLjEuMTAx//hkCABPCQAAAHyn"
)
