# Bilibili Archive Program

从[Bilibili](https://www.bilibili.com)下载内容的工具集

Utilities to download content from [Bilibili](https://www.bilibili.com)

## 依赖表

+ M	必需依赖
+ O	可选依赖
+ UNIX 指 Linux, MacOS, BSD 等**非Windows**平台，理论上兼容Windows上的MinGW / WSL，目前仅在Linux平台上验证过
+ http前端 理论上兼容所有现代浏览器，目前仅在Firefox上验证过

### 工具模块

|	项目		|	OS	|	Python3.8+	|	httpx		|	ffprobe		|
|	----		|	----	|	----		|	----		|	----		|
|	video.py	|	UNIX	|	M		|	M		|			|
|	favlist.py	|	UNIX	|	M		|	M		|			|
|	user.py		|	UNIX	|	M		|	M		|			|
|	verify.py	|	any	|	M		|			|	O		|
|	live_rec.py	|	UNIX	|	M		|	M		|			|
|	monitor.py	|	UNIX	|	M		|	M		|			|

### http后端

|	项目		|	OS	|	Python3.8+	|	FastCGI		|	httpx		|	watchdog	|
|	----		|	----	|	----		|	----		|	----		|	----		|
|	dir_listing.py	|	any	|	M		|	M		|			|			|
|	zip_access.py	|	any	|	M		|	M		|			|			|
|	video_cache.py	|	UNIX	|	M		|	M		|	M		|	M		|
|	live_status.py	|	UNIX	|	M		|	M		|	M		|			|
|	bili_proxy.py	|	any	|	M		|	M		|	M		|			|

### http前端

|	项目		|	浏览器	|	javascript	|	flv.min.js	|	hls.min.js	|
|	----		|	----	|	----		|	----		|	----		|
|	index.html	|	any	|	M		|			|			|
|	player.html	|	any	|	M		|	O		|	O		|
|	video_page.html	|	any	|	M		|			|			|
|	video_fetch.html|	any	|	M		|			|			|
|	live_page.html	|	any	|	M		|			|			|

### 桌面应用

|	项目		|	OS	|	Python3.8+	|	httpx		|	python3-gi	|
|	----		|	----	|	----		|	----		|	----		|
|	live_notify.py	|	Linux	|	M		|	M		|	M		|

### 移植提示

目前平台不兼容性主要来自以下方面
+ `multiprocessing` 创建子进程时使用`fork`方式，部分代码依赖`fork(2)`的行为
+ `core.locked_path` 使用 `flock(2)` 锁定文件
+ 使用 `AF_UNIX` 套接字进行本地通信
+ 使用 `SIGUSR1` 触发配置重载

## 使用说明

可使用 `<模块> --help` 查看参数列表

### 命令概述

+ video.py	下载/更新视频
+ favlist.py	下载收藏夹和视频
+ user.py	下载用户信息
+ verify.py	验证本地视频完整性
+ live_rec.py	直播录制
+ monitor.py	多直播间监控和录制

### 参数说明

+ -v	输出更多log
+ -q	输出更少log
+ -l	输出log到文件
+ -t	设置http超时时间
+ -s	设置请求间隔时间


## 引用

### 相关链接

+ [Bilibili](https://www.bilibili.com) 哔哩哔哩 (゜-゜)つロ 干杯~
+ [Python](https://www.python.org/) is a programming language that lets you work quickly and integrate systems more effectively.
+ [httpx](https://www.python-httpx.org/) A next-generation HTTP client for Python.
+ [FFmpeg](https://ffmpeg.org/) A complete, cross-platform solution to record, convert and stream audio and video. 
+ [FastCGI](https://pypi.org/project/fastcgi/) FastCGI and HTTP handlers for Python's socketserver classes
+ [watchdog](https://pypi.org/project/watchdog/) Python API and shell utilities to monitor file system events.
+ [flv.js](https://github.com/Bilibili/flv.js) An HTML5 Flash Video (FLV) Player written in pure JavaScript without Flash.
+ [hls.js](https://github.com/video-dev/hls.js) is a JavaScript library that implements an HTTP Live Streaming client.
+ [lighttpd](https://www.lighttpd.net/) is a secure, fast, compliant, and very flexible web server that has been optimized for high-performance environments.

### 其他链接

+ [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 哔哩哔哩-API收集整理 野生API文档 不断更新中...
+ [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) 哔哩哔哩常用API调用。支持视频、番剧、用户、频道、音频等功能。
+ [MDN Web Docs](https://developer.mozilla.org/) Documenting web technologies, including CSS, HTML, and JavaScript, since 2005.
+ [btrfs](https://btrfs.wiki.kernel.org) is a modern copy on write (COW) filesystem for Linux aimed at implementing advanced features while also focusing on fault tolerance, repair and easy administration.
