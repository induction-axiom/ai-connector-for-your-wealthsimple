// README example images: node tests_ui/screenshots.mjs
// The example conversations are illustrations with made-up numbers, not screenshots of any AI app.
import {mkdirSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {chromium} from "@playwright/test";

const OUTPUT = new URL("../docs/images/", import.meta.url);
// Every image is this many CSS pixels wide, so the README shows them at one width and text size.
const WIDTH = 1040;

// The pitch: one line on the left, one question and one answer on the right, read in a glance.
const promoCSS = `
  :root { color-scheme: light; --bg:#f5f5f7; --surface:#fff; --ink:#1d1d1f;
    --muted:#6e6e73; --line:#e8e8ed; --blue:#0071e3; }
  @media(prefers-color-scheme:dark) { :root { color-scheme:dark; --bg:#000;
    --surface:#1c1c1e; --ink:#f5f5f7; --muted:#a1a1a6; --line:#333336; --blue:#0a84ff; } }
  * {box-sizing:border-box} body {margin:0; background:var(--bg); color:var(--ink);
    font:17px/1.4 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif; -webkit-font-smoothing:antialiased}
  main {position:relative; width:${WIDTH}px; padding:64px 64px 72px; display:grid;
    grid-template-columns:1fr 420px; gap:56px; align-items:center}
  .product {margin:0 0 14px; color:var(--muted); font-size:16px; font-weight:600}
  h1 {margin:0; font-size:54px; line-height:1.04; letter-spacing:-.04em; font-weight:700}
  .user {width:fit-content; max-width:340px; margin:0 0 14px auto; padding:13px 18px;
    border-radius:22px 22px 6px 22px; background:var(--blue); color:#fff; font-size:18px}
  .answer {padding:26px 28px; border-radius:24px; background:var(--surface)}
  .figure {font-size:46px; line-height:1; font-weight:700; letter-spacing:-.035em; font-variant-numeric:tabular-nums}
  .caption {margin-top:8px; color:var(--muted); font-size:17px}
  .sample {position:absolute; right:64px; bottom:28px; margin:0; color:var(--muted); font-size:13px}
`;
// This connector's tiny visuals: the grocery store's share of the month, and the few lines of a check-in.
const visualCSS = `
  :root { --green:#1d8a3f; } @media(prefers-color-scheme:dark) { :root { --green:#30d158; } }
  .share {display:flex; height:12px; margin-top:24px; border-radius:6px; overflow:hidden; background:var(--line)}
  .share i {display:block; background:var(--blue)}
  .share-label {display:flex; justify-content:space-between; margin-top:8px; color:var(--muted); font-size:14px}
  .share-label strong {color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums}
  .lines {margin:0; padding:0; list-style:none}
  .lines li {padding:16px 0; border-top:1px solid var(--line); font-size:24px; line-height:1.15;
    font-weight:600; letter-spacing:-.02em}
  .lines li:first-child {padding-top:0; border-top:0}
  .lines li:last-child {padding-bottom:0}
  .gain {color:var(--green)}
`;
// Card spending in August after refunds, and the grocery store's part of it.
const spent = 1284.37;
const groceries = 412.60;
const examples = {
  "example-spending": `
    <section><p class="product">AI connector for your Wealthsimple</p>
      <h1>What did I spend?<br>Just ask.</h1></section>
    <section>
      <div class="user">What did I spend on my card in August?</div>
      <div class="answer">
        <div class="figure">$${Math.round(spent).toLocaleString("en-CA")}</div>
        <div class="caption">after refunds; most at the grocery store</div>
        <div class="share"><i style="width:${(groceries / spent * 100).toFixed(1)}%"></i></div>
        <div class="share-label"><span><strong>$${Math.round(groceries)}</strong> grocery store</span>
          <span>everything else</span></div>
      </div>
    </section>
    <p class="sample">Simulated example · made-up data</p>`,
  "example-weekly-checkin": `
    <section><p class="product">AI connector for your Wealthsimple</p>
      <h1>A weekly check-in,<br>written for you.</h1></section>
    <section>
      <div class="user">Every Monday, sum up my week.</div>
      <div class="answer">
        <ul class="lines"><li>Bought VEQT</li>
          <li><span class="gain">$29.48</span> in dividends</li>
          <li>Card spending up 17%</li></ul>
      </div>
    </section>
    <p class="sample">Simulated example · made-up data</p>`,
};

mkdirSync(OUTPUT, {recursive: true});
const browser = await chromium.launch();
try {
  for (const colorScheme of ["light", "dark"]) {
    // A short viewport, so the full-page capture ends where the content does.
    const context = await browser.newContext({viewport: {width: WIDTH, height: 400},
      deviceScaleFactor: 1.5, colorScheme, locale: "en-CA", reducedMotion: "reduce"});
    const page = await context.newPage();
    for (const [name, body] of Object.entries(examples)) {
      await page.setContent(`<!doctype html><html lang="en"><meta charset="utf-8">
        <title>Wealthsimple connector — simulated example</title><style>${promoCSS}\n${visualCSS}</style><main>${body}</main></html>`);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({path: fileURLToPath(new URL(`${name}-${colorScheme}.png`, OUTPUT)),
        fullPage: true, animations: "disabled"});
    }
    await context.close();
    console.log(`Generated ${Object.keys(examples).length} ${colorScheme} example images with made-up data.`);
  }
} finally {
  await browser.close();
}
