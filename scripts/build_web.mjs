import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { build } from "esbuild";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));

export const WEB_BUILD = Object.freeze({
  entry: "web_src/app.js",
  output: "llmolympic/web/static/assets/app.js",
  options: Object.freeze({
    bundle: true,
    banner: Object.freeze({
      js: "/*! LLM Olympics observer bundle; includes React/ReactDOM/Scheduler (MIT) and Modernizr 3.0.0pre Custom Build (MIT). See REACT_LICENSE.txt and THIRD_PARTY_NOTICES.md. */",
    }),
    charset: "utf8",
    define: Object.freeze({ "process.env.NODE_ENV": "\"production\"" }),
    format: "iife",
    legalComments: "eof",
    minify: true,
    platform: "browser",
    sourcemap: false,
    target: Object.freeze(["es2020"]),
    treeShaking: true,
  }),
});

export async function buildBundle() {
  const result = await build({
    ...WEB_BUILD.options,
    absWorkingDir: projectRoot,
    entryPoints: [WEB_BUILD.entry],
    logLevel: "silent",
    metafile: true,
    outfile: WEB_BUILD.output,
    write: false,
  });
  if (result.outputFiles.length !== 1) {
    throw new Error(`expected one Web bundle, received ${result.outputFiles.length}`);
  }
  return {
    contents: result.outputFiles[0].contents,
    metafile: result.metafile,
  };
}

async function main() {
  const { contents } = await buildBundle();
  const outputPath = join(projectRoot, WEB_BUILD.output);
  await mkdir(dirname(outputPath), { recursive: true });
  await writeFile(outputPath, contents);
  console.log(`built ${WEB_BUILD.output}`);
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  await main();
}
