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


## 参考部署

```sh
# 安装软件包
sudo apt install lighttpd python3-pip python3-httpx python3-watchdog

# 修改lighttpd服务
sudo mkdir -p /etc/systemd/system/lighttpd.service.d/
sudo install -m 0644 config/override.conf /etc/systemd/system/lighttpd.service.d/override.conf
sudoedit /etc/systemd/system/lighttpd.service.d/override.conf	# 修改 CODE_PATH

# 创建http目录结构
sudo mkdir -p /srv/http/code /srv/http/tmp /srv/http/html /srv/http/fcgi
sudo install -m 0664 config/lighttpd.conf /srv/http/lighttpd.conf
sudo chown root:$(id -ng) /srv/http/html /srv/http/fcgi /srv/http/lighttpd.conf
sudo chmod 0775 /srv/http/html /srv/http/fcgi

# 初始化html目录
cd /srv/http/html
for f in ../code/client/*.html
do
	ln -s $f
done
ln -s ../tmp/cache
ln -s /PATH/TO/flv.min.js
ln -s /PATH/TO/hls.min.js
# 链接其他需要通过http访问的内容

# 初始化fcgi目录
cd /srv/http/fcgi
# 通过pip3安装fastcgi
pip3 install --target /srv/http/fcgi --no-compile --no-deps fastcore fastcgi
# httpx和watchdog也可通过pip3安装
# pip3 install --target /srv/http/fcgi --no-compile httpx watchdog
for f in constants.py core.py runtime.py network.py verify.py video.py
do
	ln -s ../code/$f
done
for f in ../code/server/*.py
do
	ln -s $f
done
# 链接其他需要的FCGI接口

# 检查/修改lighttpd.conf，如仅在localhost上服务
vim /srv/http/lighttpd.conf
# 设置防火墙
# sudo ufw allow http
# 启动http服务
sudo systemctl daemon-reload
sudo systemctl restart lighttpd

```

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
