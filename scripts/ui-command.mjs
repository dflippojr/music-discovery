import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
const python = existsSync(".venv/Scripts/python.exe") ? ".venv/Scripts/python.exe"
  : existsSync(".venv/bin/python") ? ".venv/bin/python" : "python";
const child = spawn(python, ["scripts/ui_instance.py", ...process.argv.slice(2)], { stdio: "inherit" });
child.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
child.on("exit", (code) => { process.exitCode = code ?? 1; });
process.on("SIGTERM", () => child.kill("SIGTERM"));
