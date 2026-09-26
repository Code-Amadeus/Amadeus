# Amadeus 项目与资源页

独立的静态网站：项目展示、源码入门、六类资源、中英文切换、分类筛选、安装命令复制。
不构建或提供桌面安装程序。无需 npm 依赖、数据库、API 密钥或第三方字体服务。

## 本地预览

在仓库根目录运行（Python 3.10+，仅标准库）：

```powershell
python website/build.py
python -m http.server 4173 --bind 127.0.0.1 --directory build/site
```

打开 http://127.0.0.1:4173 。修改文件后重新构建并刷新页面。
生产文件位于 `build/site/`；请勿直接编辑构建结果。页面全部使用相对静态路径，
同时适用于 GitHub Pages 项目路径 `/Amadeus/` 与独立域名根目录。

## 填写网盘链接

只需编辑 `website/resources.json`。每个资源的四个网盘项初始都是 `null`，
页面显示不可点击的“待补充”。要发布一个入口，将对应项替换为：

```json
"baidu": {
  "url": "https://pan.baidu.com/s/你的真实分享链接",
  "code": "你的提取码"
}
```

其他键是 `quark`、`mega`、`google`。`code` 可省略，MEGA 的完整分享链接
（包括 `#` 后的解密部分）可直接填写。URL 必须使用 HTTPS。未提供的入口保留
`null`；不要填 `#` 或虚构下载链接。填入链接后，卡片和资源区状态自动更新。
Google Drive 若用文件夹分享，也可直接填文件夹 URL。

`title`、`description`、`detail` 分别维护中文 `zh` 和英文 `en`。
`category` 为 `voice` 或 `art`。资源 `id` 对应现有 `assets/index.json`，
构建时校验，避免网站列出不存在的安装包类型。

安装包兼容版本、大小和校验值目前没有可确认的发布信息，因此页面不虚构这些数据。
资源包准备好后，可在描述/说明中补充其已确认信息与相应文档。

## 文件与内容来源

- `index.html`：页面结构与默认中文内容。
- `styles.css`：深绿 / 薄荷绿视觉、移动端布局及减少动态效果设置。
- `app.js`：英文翻译、筛选、网盘卡片和命令复制。
- `resources.json`：网盘链接和资源文案的唯一维护入口。
- `build.py`：将页面和两张现有公开展示图复制到 `build/site/`，从
  `electron/package.json` 读取应用版本。资源清单嵌入 HTML，无需运行时请求 API。
- 角色品牌图来自 `assets/demo/amadeus-character-projection.png`；工作区截图来自
  `assets/demo/provider-runtime.jpg`。图片不在网站源码内重复保存，原有素材条款不变。

资源语义以 `assets/README.md`、`docs/external_asset_bundles.md` 和
`docs/character_pack_authoring.md` 为依据。文档链接指向公开仓库的 `main` 分支。
角色、语音、模型和演示素材的条款独立于第一方代码许可证。

## 以后发布到 GitHub Pages

此实现不自动发布。`.github/workflows/website-pages.yml` 仅手动触发，
不会因普通 push 发布网站，也不运行 Electron 打包。

准备上线后，将这些文件提交到目标仓库默认分支，在 Settings → Pages 中选择
GitHub Actions，再从 Actions 手动运行 **Website Pages**。
在当前 `Code-Amadeus/Amadeus` 仓库发布时，预计入口为
`https://code-amadeus.github.io/Amadeus/`，实际地址以部署结果为准。
如果改用组织主页仓库，可直接托管 `build/site/` 的内容；本构建脚本依赖当前
Amadeus 仓库中的版本和展示素材，不能只复制 `website/` 后直接构建。
