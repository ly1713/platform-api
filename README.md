# assets（媒体托管分支，孤儿分支）

这个分支**只放 README / 文档里引用的媒体文件**，不进 `main` 的历史，
避免把几十 MB 的视频塞进代码历史。

- `promo/stargather-promo-v9-*.mp4` — 星采宣传片 v9（1080p / 720p）
- `promo/stargather-promo-v9-poster.jpg` — 宣传片封面帧
- `promo/stargather-promo-v9-contact-sheet.jpg` — 全片接触表（8 镜静帧）

这是私有仓：`raw.githubusercontent.com` 链接不带凭据取不到文件，因此 README 与官网都不再
引用本分支，官网直接在 `site/assets/video/` 自托管同一支视频。重新生成：见工作台仓库 `promo-video/`。
