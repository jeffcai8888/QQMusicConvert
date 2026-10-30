# mflac2mp3 — OGG / FLAC / QQ 音乐转 MP3 工具

将 QQ 音乐下载的加密音频（`.mflac` / `.mgg` / `.qmcflac` / `.qmc0` 等）解密并转换为
MP3（默认 320kbps CBR，保留标签与封面，`歌手 - 歌名` 文件名自动补写 ID3 标签）。
也支持普通 `.ogg` / `.flac` 直接转 MP3，无需 QQ 音乐登录或联网。

提供 **图形界面（mflac2mp3.exe）** 与 **命令行（mflac2mp3.py）** 两种使用方式；
纯标准库实现，转码依赖 [ffmpeg](https://ffmpeg.org)。

> **下载**：Windows 免安装版 `mflac2mp3.exe` 在 [Releases](https://github.com/jeffcai8888/QQMusicConvert/releases) 页面获取；
> 转 MP3 需要 ffmpeg 在 PATH 中，或把 `ffmpeg.exe` 放在 exe 同目录。

## 快速开始

### 图形界面（推荐）

双击 `mflac2mp3.exe`（或 `python mflac_gui.py`）：

1. **添加文件 / 添加文件夹**：批量导入 OGG、FLAC 或 QQ 音乐加密音频（也可把文件拖到 exe 图标上，启动后自动进列表）；
2. 选择码率（默认 320k，或 VBR 最高音质）、输出目录（默认原目录）；
3. 点 **开始转换**——列表逐行显示进度与结果，底部有总进度条，可随时**取消**；批量结束后自动打开成功文件的输出目录（每个目录只打开一次，取消或全部失败时不打开）；
4. 选项（码率/输出目录/保留原格式音频等）会自动记忆，下次打开保持。

### 命令行

```bash
# 转换单个文件
python mflac2mp3.py "周杰伦 - 搁浅.mflac"

# 转换 MGG，保留原曲名、歌手、专辑等标签
python mflac2mp3.py "陶喆 - 普通朋友 (Live)_L.mgg"

# 普通 OGG / FLAC 直接转换（支持混合批量导入）
python mflac2mp3.py "歌手 - 歌曲.ogg" "歌手 - 另一首.flac"

# 转换整个目录（递归），输出到指定目录
python mflac2mp3.py "D:\VipSongsDownload" -o "D:\MP3"

# 保留解密出的无损 FLAC，MP3 用 VBR
python mflac2mp3.py *.mflac --keep-flac --vbr

# 算法自检（无需任何文件与网络）
python mflac2mp3.py --self-test
```

也可以把文件/文件夹**直接拖到 `convert.bat` 上**（Windows）。

MGG 通常解密为 OGG 音频，再由 ffmpeg 转成 MP3；文件选择、文件夹批量导入和拖放启动
均支持 `.mgg`（也支持 `.mgg0/.mgg1/.mggl/.mggh`）。转换时优先保留音频内的标签，
缺少歌手或曲名时才从文件名补写。`--keep-flac` 同样适用于 MGG，会保留解密出的 `.ogg`；
`--flac-only` 则只解密为原格式。OGG 通常是有损音频，转换成 320kbps MP3 不会提升原始音质。

普通 OGG / FLAC 的源文件始终保留，直接作为转码输入；“保留解密的原格式音频”选项
仅作用于加密文件。勾选“只解密不转 MP3”（`--flac-only`）时，普通音频会显示“无需解密”并跳过。

## 支持的加密格式

| 尾部格式 | 常见来源 | ekey 来源 | 是否需要联网 |
|---|---|---|---|
| QMCv1 静态加密 | 很老的 `.qmc0/.qmc3/.bkcflac` 等 | 公开静态密钥表 | 否 |
| QMCv2 + ekey 尾部 | 旧版客户端下载的 `.mflac/.mgg` | 文件尾部内嵌 | 否 |
| QMCv2 + QTag 尾部 | 旧版客户端下载 | 文件尾部内嵌 | 否 |
| QMCv2 + musicex 尾部 | **新版 PC 客户端 (≥19.57, 2023+)** | 本工具自动从本机 QQ 音乐客户端提取登录凭据，调用 `music.vkey.GetEVkey` 接口获取 | 是 |

musicex 文件本身不再内嵌密钥。工具会自动：

1. 从 `%APPDATA%\Tencent\QQMusic\QQMusicServiceConfig.ini` 读取 QQ 号 (uin)；
2. 在本机**正在运行**的 QQ 音乐进程内存中查找登录凭据 `authst`
   （仅读取同用户进程，也可用 `--authst` 手动提供）；
3. 调用 QQ 音乐官方接口获取该文件的 ekey，缓存到脚本旁 `ekey_cache.json`
   （同一文件只取一次）。

因此转换 musicex 文件的前提：**QQ 音乐客户端正在运行且已登录**，账号对对应歌曲
有下载权限（如 VIP 有效）。解密算法本身（腾讯 TEA、QMC2 Map/RC4）全部本地完成。

## 命令行选项

```
python mflac2mp3.py <文件或目录...> [选项]

  -o, --output-dir DIR   输出目录（默认与输入文件同目录）
  -b, --bitrate K        MP3 码率，默认 320
  --vbr                  使用 VBR（lame -q:a 0）代替 CBR
  --keep-flac            保留解密后的原格式音频（FLAC/OGG 等）
  --flac-only            只解密，不转 MP3
  -y, --overwrite        覆盖已存在的输出文件（默认跳过）
  --ffmpeg PATH          ffmpeg 可执行文件路径
  --no-cache             不读取/写入 ekey 缓存
  --uin NUM              手动指定 QQ 号
  --authst TOKEN         手动指定登录凭据
  --self-test            运行算法自检
```

## 关于 exe

- `mflac2mp3.exe` 为 PyInstaller 打包的**单文件图形界面**（约 12MB，无需安装 Python）。
- 杀毒软件可能对 PyInstaller 单文件程序误报。本工具确实会读取本机 QQ 音乐进程的内存来获取登录凭据
  （仅限同用户进程、仅用于换取解密密钥），源码完全公开可审阅，也可按下文自行重新打包。
- ffmpeg **不在** exe 内：exe 会依次查找 ① 手动指定路径 ② PATH ③ exe 同目录的
  `ffmpeg.exe` ④ 常见安装位置；找不到时只能解密保留原格式，界面会给出红色提示。
  最简单的离线方案：把 `ffmpeg.exe` 放到 exe 同目录。
- exe 支持把文件拖到图标上启动（自动进入列表）；也保留 `--smoke` / `--smoke-convert`
  隐藏参数用于打包自检。

### 重新打包

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name mflac2mp3 mflac_gui.py
# 产物在 dist/mflac2mp3.exe
```

### 回归测试

```bash
python -m unittest discover -s tests -v
```

测试使用合成音频，无需 QQ 音乐登录或真实歌曲。需安装 ffmpeg 和 ffprobe；覆盖 MGG 的
静态加密、内嵌 ekey、QTag、Map/RC4 路径，以及跨页 OGG 标签、缺失标签补写、普通
OGG/FLAC 转换、源文件保留和混合目录导入。

## 原理

文件结构：`[QMC 加密音频数据][尾部]`。按尾部类型分四种：

- **musicex**：尾部 192 字节含 `song_id / media_mid / filename`（UTF-16LE），
  通过 `POST https://u.y.qq.com/cgi-bin/musicu.fcg`（`music.vkey.GetEVkey`）换取 ekey。
- **QTag / ekey 尾部**：ekey（base64）直接内嵌在文件末尾。
- **无尾部**：QMCv1 固定密钥表加密。

ekey 解包：base64 →（新版可能再包一层 `QQMusic EncV2,Key:` 双层腾讯 TEA）→
前 8 字节与 `simpleMakeKey(106,8)` 交织成 TEA 密钥解出原始密钥 → 按密钥长度选择
Map（≤300 字节，预生成 0x8000 周期掩码后整块 XOR，速度很快）或 RC4（>300 字节）
流密码，对音频数据逐段异或解密。解密结果经魔数校验（fLaC / OggS / ID3 …）后交给
ffmpeg 转码。

解密正确性已用 FLAC `STREAMINFO` 内嵌的 PCM MD5 校验（逐位一致）。
`--self-test` 内置参考实现（unlock-music / qmc-decoder）的官方向量测试。

算法参考：[unlock-music](https://github.com/unlock-music/unlock-music)、
[ownlight6/qmc-decoder](https://github.com/ownlight6/qmc-decoder)。

## 已知限制

- Android 新版 `STag` 尾部格式未公开，暂不支持（会明确报错）。
- musicex 文件的 FLAC 解密结果末尾可能带有几十字节填充垃圾（QQ 音乐下载时残留），
  位于最后一帧之后，不影响播放与转码。
- RC4 长密钥路径为逐字节实现，大文件较慢（Map 路径已优化，常见文件秒级完成）。
- ekey 与登录凭据有时效性（约 22 小时），过期后重新运行即可（需客户端在线）。

## 声明

本工具仅供个人将**自己账号已购买 / 会员有效期内**下载的音乐做格式转换与备份，
请勿用于任何分发或商业用途。
