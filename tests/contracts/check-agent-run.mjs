// Validate agent-run.json files with Dvir OS's own importer (lib/review.ts).
// Usage: node check-agent-run.mjs /path/to/dvir-os file.json [file.json ...]
import {readFileSync} from "node:fs";
import {join} from "node:path";

const [dvirOs, ...files] = process.argv.slice(2);
const {parseRun} = await import(join(dvirOs, "lib", "review.ts"));
let failed = 0;
for (const file of files) {
  const result = parseRun(JSON.parse(readFileSync(file, "utf8")));
  console.log(JSON.stringify({file, ok: result.ok, errors: result.ok ? [] : result.errors}));
  if (!result.ok) failed++;
}
process.exit(failed ? 1 : 0);
