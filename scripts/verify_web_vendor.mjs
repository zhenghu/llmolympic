import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));

async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

function sha256(payload) {
  return createHash("sha256").update(payload).digest("hex");
}

function invariant(condition, message) {
  if (!condition) throw new Error(message);
}

const manifestPath = join(projectRoot, "scripts", "web_vendor_manifest.json");
const lockPath = join(projectRoot, "package-lock.json");
const manifest = await readJson(manifestPath);
const packageLock = await readJson(lockPath);

invariant(manifest.schema_version === 1, "unsupported Web vendor manifest schema");
invariant(Array.isArray(manifest.assets) && manifest.assets.length > 0, "empty Web vendor manifest");

for (const asset of manifest.assets) {
  const lockEntry = packageLock.packages[`node_modules/${asset.package}`];
  invariant(lockEntry, `package-lock entry is missing: ${asset.package}`);
  invariant(lockEntry.version === asset.version, `version mismatch for ${asset.package}`);
  invariant(lockEntry.resolved === asset.registry_tarball, `registry source mismatch for ${asset.package}`);
  invariant(lockEntry.integrity === asset.npm_integrity, `npm integrity mismatch for ${asset.package}`);

  const packageMetadata = await readJson(
    join(projectRoot, "node_modules", asset.package, "package.json"),
  );
  invariant(packageMetadata.version === asset.version, `installed version mismatch for ${asset.package}`);

  const upstream = await readFile(
    join(projectRoot, "node_modules", asset.package, asset.package_asset),
  );
  const bundled = await readFile(join(projectRoot, asset.bundled_asset));
  invariant(sha256(upstream) === asset.sha256, `upstream SHA-256 mismatch for ${asset.package}`);
  invariant(sha256(bundled) === asset.sha256, `bundled SHA-256 mismatch for ${asset.package}`);
  invariant(upstream.equals(bundled), `bundled bytes differ from ${asset.package}@${asset.version}`);
}

console.log(`verified ${manifest.assets.length} self-hosted Web vendor assets`);
