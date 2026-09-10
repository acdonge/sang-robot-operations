import { copyFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const generated = fileURLToPath(new URL("../../../ui-admin/operator.html", import.meta.url));
const mounted = fileURLToPath(new URL("../../../ui-operator/index.html", import.meta.url));

await copyFile(generated, mounted);
await rm(generated);
