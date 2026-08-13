import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { WEB_BUILD, buildBundle } from "./build_web.mjs";

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

function projectPath(path) {
  const resolved = resolve(projectRoot, path);
  invariant(
    resolved === projectRoot || resolved.startsWith(`${projectRoot}${sep}`),
    `path escapes the project root: ${path}`,
  );
  return resolved;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${stableJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function packageName(input) {
  const marker = "node_modules/";
  const start = input.lastIndexOf(marker);
  if (start === -1) return null;
  const segments = input.slice(start + marker.length).split("/");
  return segments[0].startsWith("@")
    ? `${segments[0]}/${segments[1]}`
    : segments[0];
}

const manifestPath = join(projectRoot, "scripts", "web_vendor_manifest.json");
const lockPath = join(projectRoot, "package-lock.json");
const packagePath = join(projectRoot, "package.json");
const manifest = await readJson(manifestPath);
const packageLock = await readJson(lockPath);
const packageJson = await readJson(packagePath);

invariant(manifest.schema_version === 2, "unsupported Web vendor manifest schema");
invariant(
  stableJson(manifest.build) === stableJson(WEB_BUILD),
  "manifest build configuration differs from scripts/build_web.mjs",
);
invariant(Array.isArray(manifest.packages) && manifest.packages.length > 0, "empty package list");
invariant(Array.isArray(manifest.sources) && manifest.sources.length > 0, "empty source list");
invariant(
  manifest.output?.path === WEB_BUILD.output && /^[0-9a-f]{64}$/.test(manifest.output.sha256),
  "invalid output declaration",
);

const packageNames = manifest.packages.map((item) => item.package);
invariant(new Set(packageNames).size === packageNames.length, "duplicate package declaration");

for (const item of manifest.packages) {
  invariant(["builder", "runtime"].includes(item.role), `invalid package role: ${item.package}`);
  invariant(typeof item.direct === "boolean", `invalid direct flag: ${item.package}`);
  invariant(
    item.direct === Object.hasOwn(packageJson.devDependencies ?? {}, item.package),
    `direct dependency declaration differs for ${item.package}`,
  );
  const lockEntry = packageLock.packages[`node_modules/${item.package}`];
  invariant(lockEntry, `package-lock entry is missing: ${item.package}`);
  invariant(lockEntry.version === item.version, `version mismatch for ${item.package}`);
  invariant(lockEntry.resolved === item.registry_tarball, `registry source mismatch for ${item.package}`);
  invariant(lockEntry.integrity === item.npm_integrity, `npm integrity mismatch for ${item.package}`);

  if (item.direct) {
    invariant(
      packageJson.devDependencies?.[item.package] === item.version,
      `package.json must pin ${item.package} exactly`,
    );
    invariant(
      packageLock.packages[""]?.devDependencies?.[item.package] === item.version,
      `package-lock root must pin ${item.package} exactly`,
    );
  }

  const packageMetadata = await readJson(
    join(projectRoot, "node_modules", item.package, "package.json"),
  );
  invariant(packageMetadata.version === item.version, `installed version mismatch for ${item.package}`);
}

const builders = manifest.packages.filter((item) => item.role === "builder");
invariant(
  builders.length === 1 && builders[0].package === "esbuild" && builders[0].direct,
  "esbuild must be the sole declared bundle builder",
);

const { contents, metafile } = await buildBundle();
const buildInputs = Object.keys(metafile.inputs);
const bundledPackages = [...new Set(buildInputs.map(packageName).filter(Boolean))].sort();
const runtimePackages = manifest.packages
  .filter((item) => item.role === "runtime")
  .map((item) => item.package)
  .sort();
invariant(
  stableJson(bundledPackages) === stableJson(runtimePackages),
  `bundled package set differs: expected ${runtimePackages.join(", ")}; received ${bundledPackages.join(", ")}`,
);

const localInputs = buildInputs
  .filter((input) => packageName(input) === null)
  .map((input) => relative(projectRoot, projectPath(input)).split(sep).join("/"))
  .sort();
const declaredSources = manifest.sources.map((source) => source.path).sort();
invariant(
  stableJson(localInputs) === stableJson(declaredSources),
  `local build input set differs: expected ${declaredSources.join(", ")}; received ${localInputs.join(", ")}`,
);
for (const source of manifest.sources) {
  invariant(/^[0-9a-f]{64}$/.test(source.sha256), `invalid source SHA-256: ${source.path}`);
  const payload = await readFile(projectPath(source.path));
  invariant(sha256(payload) === source.sha256, `source SHA-256 mismatch: ${source.path}`);
}

const bundled = await readFile(projectPath(manifest.output.path));
invariant(sha256(contents) === manifest.output.sha256, "reconstructed bundle SHA-256 mismatch");
invariant(sha256(bundled) === manifest.output.sha256, "committed bundle SHA-256 mismatch");
invariant(Buffer.from(contents).equals(bundled), "committed bundle differs from reconstructed output");

console.log(
  `verified reproducible Web bundle from ${runtimePackages.length} locked runtime packages`,
);
