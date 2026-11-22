import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { backendToken, startStandaloneServer } from "../frontend-entrypoint.mjs";

function temporaryFiles(t) {
  const root = mkdtempSync(path.join(tmpdir(), "autohunt-frontend-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, config: path.join(root, "runtime.env") };
}

test("reads the token from a generated shared config with CRLF", (t) => {
  const { config } = temporaryFiles(t);
  writeFileSync(config, "AUTOHUNT_API_KEY=synthetic-service-token\r\nAUTOHUNT_DATA_KEY=synthetic-encryption-key\r\n");
  assert.equal(backendToken(config), "synthetic-service-token");
});

test("missing, empty and duplicate credentials fail without including secrets", (t) => {
  const { config } = temporaryFiles(t);
  for (const content of [
    "AUTOHUNT_DATA_KEY=synthetic-encryption-key\n",
    "AUTOHUNT_API_KEY=\n",
    "AUTOHUNT_API_KEY=first-synthetic\nAUTOHUNT_API_KEY=second-synthetic\n",
  ]) {
    writeFileSync(config, content);
    assert.throws(() => backendToken(config), (error) => {
      assert.doesNotMatch(error.message, /synthetic/);
      return true;
    });
  }
});

test("passes token to the server at runtime and omits the data key", async (t) => {
  const { root, config } = temporaryFiles(t);
  const serverFile = path.join(root, "fake-server.cjs");
  const receipt = path.join(root, "receipt.json");
  writeFileSync(config, "AUTOHUNT_API_KEY=synthetic-service-token\nAUTOHUNT_DATA_KEY=synthetic-encryption-key\n");
  writeFileSync(serverFile, `require('node:fs').writeFileSync(${JSON.stringify(receipt)}, JSON.stringify({
    token: process.env.AUTOHUNT_BACKEND_TOKEN,
    apiKeyPresent: 'AUTOHUNT_API_KEY' in process.env,
    dataKeyPresent: 'AUTOHUNT_DATA_KEY' in process.env
  }));`);
  const previousApiKey = process.env.AUTOHUNT_API_KEY;
  const previousDataKey = process.env.AUTOHUNT_DATA_KEY;
  process.env.AUTOHUNT_API_KEY = "inherited-synthetic-token";
  process.env.AUTOHUNT_DATA_KEY = "inherited-synthetic-key";
  try {
    const child = startStandaloneServer(config, serverFile);
    const [code] = await once(child, "exit");
    assert.equal(code, 0);
    assert.deepEqual(JSON.parse(readFileSync(receipt, "utf8")), {
      token: "synthetic-service-token", apiKeyPresent: false, dataKeyPresent: false,
    });
  } finally {
    if (previousApiKey === undefined) delete process.env.AUTOHUNT_API_KEY;
    else process.env.AUTOHUNT_API_KEY = previousApiKey;
    if (previousDataKey === undefined) delete process.env.AUTOHUNT_DATA_KEY;
    else process.env.AUTOHUNT_DATA_KEY = previousDataKey;
  }
});
