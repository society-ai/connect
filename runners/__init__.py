"""Generic runners (docs/contribute/runners.md): a local agent type that needs only one of these is a manifest
and nothing else."""

import asyncio
import re
from typing import Optional

from core.runner import RunnerSpec, check_allowed, child_env

VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")
VERSION_TIMEOUT_S = 5


async def detect_version(spec: RunnerSpec) -> Optional[str]:
    """The version the type's detect command prints, or None. Never raises."""
    if not spec.detect:
        return None
    try:
        program = check_allowed(spec, spec.detect)
        proc = await asyncio.create_subprocess_exec(
            program, *spec.detect[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=VERSION_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - a version is display data
        return None
    match = VERSION_RE.search(out.decode(errors="replace"))
    return match.group(1) if match else None
