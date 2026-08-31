const fs = require("node:fs/promises");
const path = require("node:path");

const { chromium } = require("playwright");

const CASES = [
  {
    fileName: "beijing.png",
    city: "北京",
    subtitle: "三日步行与公共交通行程",
    attribution: "改编自 Wikivoyage 北京条目 · CC BY-SA 4.0",
    days: [
      [["09:00", "天安门广场", "东城区"], ["11:00", "天坛公园", "东城区"], ["15:00", "景山公园", "西城区"]],
      [["08:30", "颐和园", "海淀区"], ["11:30", "南锣鼓巷", "东城区"], ["15:30", "前门大街", "东城区"]],
      [["09:30", "圆明园", "海淀区"], ["13:00", "三里屯太古里", "朝阳区"], ["16:00", "北京饭店", "东城区"]],
    ],
  },
  {
    fileName: "shanghai.png",
    city: "上海",
    subtitle: "三日城市观光行程",
    attribution: "改编自 Wikivoyage 上海条目 · CC BY-SA 4.0",
    days: [
      [["09:00", "外滩", "黄浦区", "LOW_CONFIDENCE_COMBINED_CONTROL"], ["11:00", "豫园", "黄浦区"], ["14:30", "东方明珠广播电视塔", "浦东新区"]],
      [["09:30", "田子坊", "黄浦区"], ["12:30", "新天地", "黄浦区"], ["15:30", "上海迪士尼乐园", "浦东新区"]],
      [["09:00", "七宝古镇", "闵行区"], ["13:00", "上海闵行文化公园", "闵行区"], ["16:00", "闵行博物馆", "闵行区"]],
    ],
  },
  {
    fileName: "hangzhou.png",
    city: "杭州",
    subtitle: "三日湖城行程",
    attribution: "改编自 Wikivoyage 杭州条目 · CC BY-SA 4.0",
    days: [
      [["08:30", "西湖风景名胜区", "西湖区"], ["11:00", "灵隐寺", "西湖区"], ["15:00", "雷峰塔", "西湖区"]],
      [["09:00", "西溪湿地国家公园", "西湖区"], ["12:30", "河坊街·清河坊", "上城区"], ["15:30", "龙井村（茶园）", "西湖区"]],
      [["08:30", "楼外楼（孤山路店）", "西湖区"], ["12:00", "知味观（仁和店）", "上城区"], ["15:00", "西湖醋鱼（楼外楼分部）", "西湖区"]],
    ],
  },
];

const STYLE = `
  * { box-sizing: border-box; }
  html, body { margin: 0; width: 1080px; height: 1920px; overflow: hidden; }
  body {
    padding: 51px 62px;
    background: #f4f7fb;
    color: #101a31;
    font-family: "Microsoft YaHei", "Noto Sans SC", sans-serif;
  }
  .title { margin: 0; font-size: 66px; font-weight: 800; line-height: 1.05; }
  .subtitle { margin: 5px 0 14px; color: #35517d; font-size: 30px; line-height: 1.4; }
  .attribution { margin-bottom: 14px; color: #506b98; font-size: 20px; }
  .rule { height: 4px; margin-bottom: 33px; background: #416bd4; }
  section {
    height: 466px;
    margin-bottom: 30px;
    padding: 21px 31px 24px;
    border: 2px solid #d6e0f0;
    border-radius: 24px;
    background: #fff;
    box-shadow: 0 8px 24px rgba(29, 55, 105, 0.04);
  }
  h2 { margin: 0 0 13px; color: #2051aa; font-size: 39px; line-height: 1.15; }
  .items { display: flex; gap: 18px; }
  article {
    position: relative;
    width: 285px;
    height: 361px;
    padding: 20px 19px;
    border-left: 7px solid #6d91ee;
    border-radius: 15px;
    background: #f7f9fd;
  }
  time {
    display: block;
    margin-bottom: 29px;
    color: #435b86;
    font-size: 28px;
    font-weight: 700;
    line-height: 1.2;
  }
  strong { position: relative; display: block; color: #071327; font-size: 26px; font-weight: 800; line-height: 1.34; }
  strong span { position: relative; z-index: 1; }
  strong i {
    position: absolute;
    z-index: 2;
    top: 0;
    width: 3px;
    height: 35px;
    background: #f7f9fd;
  }
  strong i:nth-of-type(1) { left: 65px; width: 4px; }
  strong i:nth-of-type(2) { left: 85px; width: 4px; }
  strong i:nth-of-type(3) { left: 105px; width: 4px; }
  strong i:nth-of-type(4) { left: 125px; }
  .area {
    position: absolute;
    right: 19px;
    bottom: 101px;
    left: 19px;
    height: 57px;
    overflow: hidden;
    border-radius: 9px;
    background: #e8eef9;
    color: #506892;
    font-size: 27px;
    line-height: 57px;
    text-align: center;
  }
  .area span { position: relative; z-index: 1; }
  .footer { margin-top: 24px; padding-right: 20px; color: #59719d; font-size: 18px; text-align: right; }
`;

function htmlFor(fixture) {
  const cards = fixture.days
    .map((items, dayIndex) => {
      const entries = items
        .map(([time, place, area, control]) => {
          const combined = control === "LOW_CONFIDENCE_COMBINED_CONTROL";
          const renderedPlace = combined ? `${place}·${area}` : place;
          const occlusion = combined ? "<i></i><i></i><i></i><i></i>" : "";
          const areaProjection = combined ? "" : `<div class="area"><span>${area}</span></div>`;
          return `<article><time>${time}</time><strong><span>${renderedPlace}</span>${occlusion}</strong>${areaProjection}</article>`;
        })
        .join("");
      return `<section><h2>Day ${dayIndex + 1}</h2><div class="items">${entries}</div></section>`;
    })
    .join("");
  return `<!doctype html><html><head><meta charset="utf-8"><style>${STYLE}</style></head><body><h1 class="title">${fixture.city}</h1><div class="subtitle">${fixture.subtitle}</div><div class="attribution">${fixture.attribution}</div><div class="rule"></div>${cards}<div class="footer">项目评测派生文本 · 无联系方式 · 无嵌入媒体</div></body></html>`;
}

async function main() {
  const outputArgument = process.argv[2];
  if (!outputArgument) {
    throw new Error("Usage: node render-g04-parity-fixtures.js <external-output-directory>");
  }
  const outputDirectory = path.resolve(outputArgument);
  const repositoryRoot = path.resolve(__dirname, "../../..");
  const relative = path.relative(repositoryRoot, outputDirectory);
  if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
    throw new Error("G04 source screenshots must remain outside the Git workspace");
  }
  await fs.mkdir(outputDirectory, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
    for (const fixture of CASES) {
      await page.setContent(htmlFor(fixture), { waitUntil: "load" });
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: path.join(outputDirectory, fixture.fileName), type: "png" });
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
