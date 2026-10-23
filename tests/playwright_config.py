"""Load a Playwright config the way Playwright would, without node_modules."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Stubs out @playwright/test so the test job can read a config without npm ci.
LOAD_CONFIG = """
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
const stub = "export const defineConfig = (c) => c;"
  + "export const devices = new Proxy({}, { get: () => ({}) });";
registerHooks({ resolve: (specifier, context, next) => specifier === "@playwright/test"
  ? { url: "data:text/javascript," + encodeURIComponent(stub), shortCircuit: true }
  : next(specifier, context) });
const config = (await import(pathToFileURL(process.env.CONFIG).href)).default;
console.log(JSON.stringify(config));
"""


def load_playwright_config(path, **env):
    """Run node on the config at ROOT/path with only the given UI variables set."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in ("UI_PORT", "VERIFY_BASE_URL")
    }
    return subprocess.run(
        ["node", "--input-type=module", "-e", LOAD_CONFIG],
        cwd=ROOT,
        env={**environ, "CONFIG": str(ROOT / path), **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
