/**
 * M1 门禁 · 校验浏览器端投影与 Python 端逐位一致
 * ==============================================
 * 读 web/golden-projection.json（由 src/gen_golden.py 用 Python 生成），
 * 用 web/histmap.js 的实现跑同一组输入并比对。不一致则退出码 1，可挂 CI。
 *
 *     python src/gen_golden.py && node web/verify.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
// Windows 上动态 import 必须给 file:// URL，直接给 D:\... 会报
// ERR_UNSUPPORTED_ESM_URL_SCHEME
const m = await import(pathToFileURL(join(here, "histmap.js")).href);
const { Viewport, getProjection } = m;

const golden = JSON.parse(readFileSync(join(here, "golden-projection.json"), "utf8"));
const tol = golden.tolerance ?? 1e-6;

let total = 0, bad = 0, worst = 0, worstAt = "";
console.log(`容差 ${tol}  ·  ${golden.cases.length} 个用例\n`);

for (const c of golden.cases) {
  const proj = new (getProjection(c.projection))(c.bbox, new Viewport(...c.viewport), c.padding);
  let maxD = 0;
  for (const p of c.points) {
    const [x, y] = proj.at(p.lon, p.lat);
    const dx = Math.abs(x - p.x), dy = Math.abs(y - p.y);
    const d = Math.max(dx, dy);
    total++;
    maxD = Math.max(maxD, d);
    if (d > tol) {
      bad++;
      console.log(`  ✗ ${c.id} (${p.lon},${p.lat})  期望 (${p.x.toFixed(6)}, ${p.y.toFixed(6)})`);
      console.log(`                        实得 (${x.toFixed(6)}, ${y.toFixed(6)})  Δ=${d.toExponential(2)}`);
    }
  }
  if (maxD > worst) { worst = maxD; worstAt = c.id; }
  console.log(`  ${bad ? "✗" : "✓"} ${c.id.padEnd(22)} ${c.points.length} 点  最大偏差 ${maxD.toExponential(2)}`);
}

console.log(`\n${total - bad}/${total} 点通过   全局最大偏差 ${worst.toExponential(2)} (${worstAt})`);
if (bad) {
  console.log("\nM1 未通过：两端投影不一致，禁止进入下一步。");
  process.exit(1);
}
console.log("M1 通过：浏览器端投影与 Python 端逐位一致。");
