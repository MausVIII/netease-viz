# ATRI 顶栏音频可视化

GNOME 顶栏上的一个小挂件：**左边跟着网易云跳的线条，右边一句一句滚动的歌词**。

![整条顶栏](preview/整条顶栏.png)

## 长什么样

- **左边**：细线以顶栏竖直中线为轴上下对称伸缩，颜色沿调色板随振幅**线性**过渡，
  带**动态模糊拖尾**（多层历史帧残影 + 线尖柔化）。宽度默认 236px。
- **右边**：中文歌单栏；**非中文歌双栏 —— 左中文（翻译）、右原词**，
  整块在「时钟 → 状态图标」之间的空档里**居中**。
- 换句时：上句**向上平移淡出**，下句从**下方慢慢升起**。
- 字体：机器上的「萝莉体 第二版」，缺字交给 Pango 兜底。
- **跟着网易云**：MPRIS 里网易云一出现就显示，退出后自动隐藏（暂停还会留 12 秒）。
- **不遮时间、不遮状态**：都是顶栏面板的真实子控件，并实时算宽度预算——
  不够就先缩歌词、再缩线条，实在放不下整体隐藏。

![歌词双栏](preview/歌词-双栏.png)

## 结构

```
atri-visualizer/                    ← 本目录就是仓库（改完跑 ./install.sh 部署）
├── extension/                      GNOME Shell 扩展源码
│   ├── extension.js                挂载、读状态、动画、右键菜单、宽度预算
│   ├── bars.js                     线条绘制（扩展与离屏预览共用同一份）
│   └── stylesheet.css              萝莉体等样式
├── atri_visd.py                    后台服务：把「该画什么」写进 state.json
├── audiobands.py                   音频：抓默认输出的 monitor → FFT → 24 段
├── lyrics.py                       歌词：MPRIS → 网易云 API → LRC → 当前句
├── install.sh                      部署扩展 + 装服务 + 预热启用列表
├── systemd/atri-visualizer.service systemd --user 单元
├── tools/                          开发工具（见文末）
└── preview/                        截图

运行时产物：
  ~/.local/share/gnome-shell/extensions/atri-visualizer@maus.local/   部署后的扩展
  ~/.config/atri-visualizer/config.json                               偏好设置
  $XDG_RUNTIME_DIR/atri-visualizer/state.json                         后台 → 扩展的唯一通道
```

分工很干净：**后台只算不画，扩展只画不算**。后台挂了顶栏只是安静地隐藏，不报错、不刷屏。

## 安装 / 更新

```bash
cd ~/.local/share/atri-visualizer
./install.sh                 # 部署扩展 + 重启后台 + 设为随会话自启
./install.sh --no-autostart  # 不想开机自启后台就用这个
```

改完 `extension/` 里的代码，重跑一次 `./install.sh` 就会部署到 GNOME 的扩展目录。
但 **GNOME 只在登录时加载扩展代码**，正在跑的 Shell 会一直用旧版本，
所以改完要**注销重登**一次才生效（Wayland 下没法热重启 shell）。

卸载：

```bash
systemctl --user disable --now atri-visualizer
gnome-extensions disable atri-visualizer@maus.local
```

## 用法

**右键点线条**（或 Ctrl+左键）出菜单：

| 菜单项 | 说明 |
|---|---|
| 显示/隐藏歌词、歌词位置 | 顶栏右侧（自动）/ 线条右边 |
| 显示/隐藏翻译 | 关掉就永远单栏 |
| 调色板 | 青→粉 / 彩虹 / 冰蓝 / 樱→紫 |
| 线条宽度 | 176 / 236 / 300 / 380 px |
| 灵敏度 | 标准 / 灵敏 / 很灵敏 / 沉稳 |
| 歌词宽度 | 只在"线条右边"模式生效 |
| 歌词延迟 ±0.2s | 写进 config.json，后台每秒读一次 |
| 重启后台服务 | 后台卡住时用 |

## 音频这条路（顺便记几个坑）

1. **只抓输出设备的 monitor，绝不碰麦克风**。`pw-record` 必须带
   `-P stream.capture.sink=true`，否则它会无视 `--target` 直接连默认输入设备。
   现在还有一道保险：采集起来后查一眼 `pw-link`，发现连到输入设备立刻掐断。
2. **延迟**：`pw-record` 默认 100ms 缓冲，实测端到端 92ms；换成 `--latency 10ms`
   后实测 36ms。
3. **FFT**：1024 点汉宁窗、512 点步进，零填充到 2048 点后切成 48 个近似对数频段
   （零填充只为让低频那几段分得开，不增加延迟）。
4. **自适应增益**：每段各自跟一条"噪声底"和一条"峰值参照"（慢慢爬 / 慢慢落），
   归一化后再压一次小信号（`CONTRAST`），这样压缩过的流行歌也不会整排发亮。
5. **换设备自动重连**：插耳机、连蓝牙会改默认输出，后台每 2 秒复查一次目标。

## 歌词这条路

1. 会话总线上找 `org.mpris.MediaPlayer2.*`，优先网易云；
2. 取歌名/歌手/时长/播放位置（位置用本地时钟外推，切句误差 < 50ms）；
3. 只搜歌名，再按时长（±3s）+ 歌手挑出正确的 song id，拉 `/api/song/lyric`；
4. 解析 LRC（多时间标签、`[offset:]`、过滤"作词/作曲/编曲…"这类制作信息），
   结果缓存到 `~/.cache/atri-visualizer/lyrics/`。

## 排错

| 现象 | 先看这里 |
|---|---|
| 顶栏什么都没有 | 网易云在放吗？`cat $XDG_RUNTIME_DIR/atri-visualizer/state.json` 看 `visible` |
| 有歌词没线条 | `python3 audiobands.py --probe` 能不能解出默认输出 |
| 线条不动/发平 | `journalctl --user -u atri-visualizer -n 30`；换设备后是否重连上了 |
| 字体不对 | `fc-match "萝莉体 第二版"`；St 只认 `font: 15px "萝莉体 第二版"` 这种写法 |
| 想开扩展日志 | `touch ~/.cache/atri-visualizer/debug` 后看 `journalctl --user -b -o cat /usr/bin/gnome-shell \| grep atri` |
| 网易云自己打不开 | `python3 tools/fix_netease_playlist.py --check`（上游删歌 bug，见下） |

## 开发工具

```bash
python3 audiobands.py --probe                       # 解析到的输出设备
python3 audiobands.py --test --fps 60               # 合成频段，不碰真实音频
python3 tools/fake_mpris.py --seconds 60            # 假网易云（测整条链路）
gjs -m tools/render_preview.mjs /tmp/x.png rainbow  # 离屏画线条，验收配色/拖尾
gjs -m tools/render_lyrics.mjs /tmp/y.png           # 离屏画歌词，验字体
bash tools/smoke/smoke.sh                           # 隔离的无头 GNOME Shell：真加载、真截图
ATRI_REAL_RUNTIME=1 bash tools/smoke/smoke.sh       # 同上，但读真后台的 state.json
```

`tools/smoke/` 起的是**独立会话**（私有 D-Bus + 独立 XDG_DATA_HOME + 内存版 GSettings），
截图只落在 `/tmp`，不碰真实桌面。

## 已知边界

- 只认网易云播放器，别的 MPRIS 播放器会被忽略（改 `lyrics.py` 的 `NETEASE_HINTS` 可放宽）。
- 改完扩展代码要注销重登才生效。
- 网易云 GTK4 客户端 2.5.4 自身有个 bug：随机播放模式下删歌会让存档里 `list` 与
  `shuffle` 长度不一致，下次启动直接 panic 打不开。附带的
  `tools/fix_netease_playlist.py` 能一键修好（会先备份）。
