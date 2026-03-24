/**
 * Monaco loads from jsDelivr (avoids huge Vite worker bundles).
 * First open of Notes needs network access to cdn.jsdelivr.net.
 */
import { loader } from "@monaco-editor/react";

const MONACO_VERSION = "0.55.1";

loader.config({
  paths: {
    vs: `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`,
  },
});

/** Await this before mounting <Editor /> so the loader is ready. */
export const monacoReady = loader.init();
