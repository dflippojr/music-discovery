// The disposable UI instance listens only on http://127.0.0.1:$UI_PORT (default 4173).
// scripts/ui_instance.py applies the same rule; tests/test_ui_instance.py keeps them equal.
export const DEFAULT_UI_PORT = 4173;

export function uiPort(env = process.env) {
  const raw = env.UI_PORT ?? "";
  if (raw === "") return DEFAULT_UI_PORT;
  const port = Number(raw);
  if (!/^[0-9]+$/.test(raw) || port < 1024 || port > 65535) {
    throw new Error(`UI_PORT must be a whole number from 1024 to 65535, got ${JSON.stringify(raw)}`);
  }
  return port;
}
