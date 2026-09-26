"use strict";

const resources = JSON.parse(
  document.getElementById("resource-data").textContent,
);
const repository = "https://github.com/Code-Amadeus/Amadeus/blob/main/";
const english = {
  skip: "Skip to main content",
  navigation: "Main navigation",
  navExperience: "Experience",
  navResources: "Resources",
  navStart: "Get started",
  heroFirst: "By your side.",
  heroSecond: "On your team.",
  heroDescription:
    "Talk, share a space, and get things done. Real-time voice, character expression and agent collaboration, together on your desktop.",
  exploreResources: "Explore resources",
  watchDemo: "Watch demo",
  sourceAvailable: "Open source · A work in progress",
  heroAlt: "Amadeus character brand illustration in mint-green halftone",
  brandArtwork: "Brand artwork",
  coreExperience: "Core experience",
  principleTalk: "Connect",
  principleEmbody: "Be present",
  principleAct: "Delegate",
  principleControl: "Stay in charge",
  experienceTitle: "Beyond the conversation.",
  experienceIntro:
    "From a few words to a finished task.\nSee the process. Stay in control.",
  demoAria: "Watch the Amadeus demo on Bilibili",
  workspaceAlt:
    "The Amadeus workspace showing a character, task progress and results",
  fullDemo: "Watch the full demo",
  featureVoice: "A two-way conversation",
  featureVoiceBody:
    "Speak or type naturally, and interrupt when you need to. Subtitles, lip sync and expressions follow actual speech playback.",
  featureWork: "Work you can follow",
  featureWorkBody:
    "Delegate tasks to specialized agents. Follow progress, artifacts and results in the same workspace.",
  featureControl: "Your call, throughout",
  featureControlBody:
    "Permission requests stay visible. Continue, retry or take over to keep the collaboration in your hands.",
  demoNote:
    "Footage shows a project prototype. The interface is evolving; see the documentation for current capabilities and configuration.",
  resourcesTitle: "A voice. A character.\nA space of your own.",
  resourcesIntro:
    "Choose the voice, character and scene resources you need.\nStart with text, then make your desktop your own.",
  resourcesNotice:
    "Resource links are being prepared. Cloud storage links will be added here. These are independent asset packs, not desktop installers.",
  installGuide: "Installation guide",
  filterAria: "Filter resources by type",
  filterAll: "All resources",
  filterVoice: "Voice & models",
  filterArt: "Characters & art",
  resourceList: "Resource list",
  resourceNote:
    "All packs are optional. Text Chat and Work remain available without a character pack. Local voice also requires compatible dependencies and hardware.",
  rightsTitle: "Kurisu character & voice notice",
  rightsOwnership:
    "Kurisu Makise originates from STEINS;GATE, whose original-work copyright credit is ©MAGES./NITRO PLUS. Rights in the character, design and related artwork remain with their respective rights holders. Rights in the original voice recordings and performances remain with their respective rights holders.",
  rightsUsage:
    "Kurisu-related character and voice content in this project is intended solely for non-commercial technical learning, research and demonstration. Third parties must not use the related character assets, voice resources or derivative content for sale, paid distribution, commercial promotion or other profit-making dissemination.",
  rightsScope:
    "This is an unofficial project. It does not represent the original rights holders or voice actors, nor does it imply their authorization or endorsement. This notice grants no asset license; educational or non-commercial use does not itself establish permission. Use and redistribution remain subject to the rights holders’ permissions and applicable law. These assets are not licensed under the Amadeus source-code license.",
  rightsSource: "Official copyright credit ↗",
  startTitle: "Make the first connection.",
  startIntro:
    "Explore the current Source Alpha.\nStart with the basics, then add what you need.",
  stepOne: "Run from source",
  stepOneBody:
    "Follow the setup guide for your system. Begin with L1 text Chat and Work, then configure your models and execution providers.",
  quickstart: "Quick start",
  stepTwo: "Choose your experience",
  stepTwoBody:
    "Pick an installation profile for voice. Voice, character and scene packs are separate, so you can add only what you need.",
  profiles: "Installation profiles",
  stepThree: "Install, then connect",
  stepThreeBody:
    "Verify and install asset bundles from the project directory. Check your models, voice and character status in Settings.",
  assetGuide: "Asset installation guide",
  terminalDescription:
    "Replace asset-bundle.zip with the path to your downloaded pack. Run from the repository root.",
  terminalLabel: "After completing the base setup",
  copy: "Copy commands",
  communityTitle: "Build what comes next.",
  communityBody:
    "Explore the source, share feedback or start creating your own character resources.",
  sourceCode: "Explore the source",
  feedback: "Issues & feedback",
  authoring: "Character authoring guide",
  licenseNote:
    "First-party code is licensed under AGPL-3.0. Characters, voices, models and demo media retain their own terms; the code license does not grant rights to use or redistribute them.",
  license: "Code license ↗",
};

// Chinese remains in the HTML so the main page works before JavaScript runs.
const chinese = {};
for (const element of document.querySelectorAll("[data-i18n]")) {
  chinese[element.dataset.i18n] = element.innerText;
}
for (const [attribute, dataKey] of [
  ["alt", "i18nAlt"],
  ["aria-label", "i18nAria"],
]) {
  for (const element of document.querySelectorAll(
    `[data-${dataKey.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}]`,
  )) {
    chinese[element.dataset[dataKey]] = element.getAttribute(attribute);
  }
}
const ui = {
  zh: {
    pending: "待补充",
    ready: "已有链接",
    preparing: "链接准备中",
    voice: "语音与模型",
    art: "角色与美术",
    guide: "查看资源说明",
    code: "提取码",
    count: (n) => `${String(n).padStart(2, "0")} 项资源`,
    copied: "已复制命令",
    copyFailed: "复制未完成，请选中上方命令手动复制。",
    noticeReady:
      "部分资源已提供网盘链接，其余入口仍在准备中。资源包独立安装，不包含桌面安装程序。",
    mirrors: {
      baidu: "百度网盘",
      quark: "夸克网盘",
      mega: "MEGA",
      google: "Google Drive",
    },
  },
  en: {
    pending: "Coming soon",
    ready: "Links available",
    preparing: "Links pending",
    voice: "VOICE & MODELS",
    art: "CHARACTERS & ART",
    guide: "Read pack documentation",
    code: "Access code",
    count: (n) => `${String(n).padStart(2, "0")} RESOURCES`,
    copied: "Commands copied",
    copyFailed:
      "Could not copy. Select the commands above and copy them manually.",
    noticeReady:
      "Some resource links are available; the remaining links are being prepared. These are independent asset packs, not desktop installers.",
    mirrors: {
      baidu: "Baidu Pan",
      quark: "Quark Pan",
      mega: "MEGA",
      google: "Google Drive",
    },
  },
};
let language = "zh";
let activeFilter = "all";
try {
  if (localStorage.getItem("amadeus-site-language") === "en") language = "en";
} catch {
  /* Storage is optional. */
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function resourceVisual(resource, index) {
  const visual = element("div", `resource-visual visual-${resource.visual}`);
  visual.setAttribute("aria-hidden", "true");
  visual.append(
    element("span", "visual-code", resource.label),
    element(
      "span",
      "visual-index",
      `${String(index + 1).padStart(2, "0")} / ASSET`,
    ),
  );
  const symbol = element("span", "resource-symbol");
  if (["voice", "asr", "experimental"].includes(resource.visual)) {
    for (const height of [9, 17, 28, 20, 39, 48, 31, 19, 38, 46, 26, 17, 9]) {
      const bar = element("i");
      bar.style.setProperty("--bar", `${height}px`);
      symbol.append(bar);
    }
  } else {
    symbol.append(
      element(
        "span",
        {
          character: "sprite-box",
          scene: "scene-symbol",
          portrait: "portrait-symbol",
        }[resource.visual],
      ),
    );
  }
  visual.append(symbol);
  return visual;
}

function renderResources() {
  const labels = ui[language];
  const grid = document.getElementById("resource-grid");
  const fragment = document.createDocumentFragment();
  const filtered = resources.filter(
    (resource) => activeFilter === "all" || resource.category === activeFilter,
  );
  for (const resource of filtered) {
    const card = element("article", "resource-card");
    const titleId = `resource-${resource.id}`;
    card.setAttribute("aria-labelledby", titleId);
    card.append(resourceVisual(resource, resources.indexOf(resource)));
    const body = element("div", "resource-body");
    const topline = element("div", "resource-topline");
    const available = Object.values(resource.mirrors).some(Boolean);
    topline.append(
      element("span", "", labels[resource.category]),
      element(
        "span",
        `pending-badge${available ? " ready-badge" : ""}`,
        available ? labels.ready : labels.preparing,
      ),
    );
    const title = element("h3", "", resource.title[language]);
    title.id = titleId;
    body.append(
      topline,
      title,
      element("p", "resource-description", resource.description[language]),
      element("p", "resource-detail", resource.detail[language]),
    );
    const mirrors = element("div", "mirror-list");
    for (const [provider, mirror] of Object.entries(resource.mirrors)) {
      const wrapper = element("div", "mirror-wrap");
      const link = element(
        mirror ? "a" : "span",
        `mirror${mirror ? " available" : ""}`,
      );
      if (mirror) {
        link.href = mirror.url;
        link.setAttribute(
          "aria-label",
          `${resource.title[language]} · ${labels.mirrors[provider]}`,
        );
      } else {
        link.setAttribute("aria-disabled", "true");
      }
      link.append(
        element("span", "", labels.mirrors[provider]),
        element("small", "", mirror ? "↗" : labels.pending),
      );
      wrapper.append(link);
      if (mirror?.code)
        wrapper.append(
          element("span", "mirror-code", `${labels.code}: ${mirror.code}`),
        );
      mirrors.append(wrapper);
    }
    body.append(mirrors);
    const guide = element("a", "resource-doc", `${labels.guide} ↗`);
    guide.href = `${repository}docs/external_asset_bundles.md`;
    body.append(guide);
    card.append(body);
    fragment.append(card);
  }
  grid.replaceChildren(fragment);
  document.getElementById("resource-count").textContent = labels.count(
    filtered.length,
  );
  if (
    resources.some((resource) => Object.values(resource.mirrors).some(Boolean))
  ) {
    document.querySelector('[data-i18n="resourcesNotice"]').textContent =
      labels.noticeReady;
  }
}

function setLanguage(nextLanguage) {
  language = nextLanguage;
  const translation = language === "en" ? english : chinese;
  document.documentElement.lang = language === "en" ? "en" : "zh-CN";
  document.title =
    language === "en"
      ? "Code Amadeus — Project & Resources"
      : "Code Amadeus — 项目与资源中心";
  document.querySelector('meta[name="description"]').content =
    language === "en"
      ? "Code Amadeus: a real-time multimodal desktop agent. Explore the project, run from source and find voice, character and scene resources."
      : "Code Amadeus：实时多模态桌面智能体。探索项目、从源码开始，获取语音、角色与场景资源。";
  for (const node of document.querySelectorAll("[data-i18n]")) {
    const value = translation[node.dataset.i18n];
    node.replaceChildren();
    value.split("\n").forEach((line, index) => {
      if (index) node.append(document.createElement("br"));
      node.append(document.createTextNode(line));
    });
  }
  for (const [selector, key, attribute] of [
    ["[data-i18n-alt]", "i18nAlt", "alt"],
    ["[data-i18n-aria]", "i18nAria", "aria-label"],
  ]) {
    for (const node of document.querySelectorAll(selector))
      node.setAttribute(attribute, translation[node.dataset[key]]);
  }
  const toggle = document.getElementById("language-toggle");
  toggle.textContent = language === "en" ? "中文 ↗" : "EN ↗";
  toggle.setAttribute(
    "aria-label",
    language === "en" ? "切换为中文" : "Switch to English",
  );
  document.querySelector('[data-doc="quickstart"]').href =
    `${repository}${language === "en" ? "README.md#quick-start" : "README_ZH.md#快速开始"}`;
  document.getElementById("copy-status").textContent = "";
  try {
    localStorage.setItem("amadeus-site-language", language);
  } catch {
    /* Storage is optional. */
  }
  renderResources();
}

document
  .getElementById("language-toggle")
  .addEventListener("click", () =>
    setLanguage(language === "zh" ? "en" : "zh"),
  );
for (const button of document.querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    for (const filter of document.querySelectorAll("[data-filter]"))
      filter.setAttribute("aria-pressed", String(filter === button));
    renderResources();
  });
}
document.getElementById("copy-command").addEventListener("click", async () => {
  const status = document.getElementById("copy-status");
  try {
    await navigator.clipboard.writeText(
      document.getElementById("install-command").textContent,
    );
    status.textContent = ui[language].copied;
  } catch {
    status.textContent = ui[language].copyFailed;
  }
});
setLanguage(language);
