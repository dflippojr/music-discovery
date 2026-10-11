import { spawnSync } from "node:child_process";
export default async function teardown() {
  const result = spawnSync(process.execPath, ["scripts/ui-command.mjs", "stop"], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || "UI teardown failed");
  console.log(result.stdout.trim());
}
