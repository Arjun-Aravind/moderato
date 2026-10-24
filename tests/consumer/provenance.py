"""Identify an installed consumer and verify its local wheel's runtime contents."""

import hashlib
import importlib.metadata
import json
import os
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


def installed():
    distribution = importlib.metadata.distribution("moderato")
    direct = json.loads(distribution.read_text("direct_url.json"))
    source = urlsplit(direct["url"])
    assert source.scheme == "file", "Install the qualification wheel from a local file"
    wheel = Path(unquote(source.path))
    assert wheel.suffix == ".whl"
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("moderato/") and not name.endswith("/"):
                path = Path(distribution.locate_file(name)).resolve()
                assert path.is_relative_to(Path(sys.prefix).resolve())
                assert path.read_bytes() == archive.read(name), name
    return {
        "environment_prefix": str(Path(sys.prefix).resolve()),
        "application_prefix": os.environ["QUALIFICATION_PREFIX"],
        "profile": os.environ["QUALIFICATION_PROFILE"],
        "wheel": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
