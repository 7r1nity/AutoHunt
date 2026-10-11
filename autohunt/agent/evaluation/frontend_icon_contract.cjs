// Offline SSR contract; does not exercise an installed browser extension.
// Run: node agent/evaluation/frontend_icon_contract.cjs (from autohunt/).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const vm = require("node:vm");

const frontend = path.resolve(__dirname, "../../frontend");
const frontendRequire = createRequire(path.join(frontend, "package.json"));
const ts = frontendRequire("typescript");
const React = frontendRequire("react");
const { renderToStaticMarkup } = frontendRequire("react-dom/server");
const source = fs.readFileSync(path.join(frontend, "components/workspace-icon.tsx"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
}).outputText;
const iconExports = {};
vm.runInNewContext(compiled, { exports: iconExports, require: frontendRequire });
const Icon = iconExports.WorkspaceIcon;
const names = ["overview", "chat", "board", "profile", "insights", "plans", "settings", "arrow", "search", "check", "send", "plus"];
let checks = 0;
for (const name of names) {
  const render = () => renderToStaticMarkup(React.createElement(Icon, { name }));
  const markup = render();
  assert.equal(markup, render(), `${name}: deterministic markup`);
  assert.match(markup, /class="workspace-icon"/, `${name}: CSS stroke class`);
  assert.doesNotMatch(markup, /\s(?:stroke|style)=/, `${name}: no inline stroke/color for extension to rewrite`);
  assert.match(markup, /width="20" height="20" viewBox="0 0 24 24"/, `${name}: dimensions`);
  assert.match(markup, /fill="none" stroke-width="1\.7" stroke-linecap="round" stroke-linejoin="round"/, `${name}: shape styling`);
  assert.match(markup, /aria-hidden="true" focusable="false"/, `${name}: decorative semantics`);
  assert.match(markup, /<path d="[^"]+"/, `${name}: path retained`);
  checks += 7;
}
const custom = renderToStaticMarkup(React.createElement(Icon, { name: "chat", className: "custom", style: { marginTop: 2 } }));
assert.match(custom, /class="workspace-icon custom"/);
assert.match(custom, /style="margin-top:2px"/);
const css = fs.readFileSync(path.join(frontend, "app/globals.css"), "utf8");
assert.match(css, /\.workspace-icon\s*\{\s*stroke:\s*currentColor;\s*\}/);
checks += 3;
process.stdout.write(`PASS: ${checks} icon SSR/CSS checks; no browser-extension E2E claim.\n`);
