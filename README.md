# Bilibili Archive Program

从[Bilibili](https://www.bilibili.com)下载内容的工具集

Utilities to download content from [Bilibili](https://www.bilibili.com)


## 工具模块使用说明


### 模块概述

+ video.py	下载视频，更新本地视频存档
+ favlist.py	下载收藏夹信息和视频
+ user.py	下载用户信息和投稿视频
+ verify.py	验证本地视频完整性
+ live_rec.py	直播录制，支持FLV/HLS直播流，支持录制弹幕
+ monitor.py	直播间批量监控和录制

### 常见参数说明

每个模块的完整参数列表请使用 `<模块> --help` 查看


|	参数	|	短参数	|	功能	|	备注	|
|	----	|	----	|	----	|	----	|
|	--root	|	-r	|	设置下载根目录，不同类型的内容将分别输出在对应子目录中	|	默认为当前工作目录	|
|	--dir	|	-d	|	设置输出目录	|	覆盖 -r 选项	|
|	--credential	|	-u	|	传入描述登录状态的credential文件	|	|
|	--mode	|	-m	|	设置视频下载模式：fix, update, force	|	|
|	--ignore	|	|	忽略指定类型的内容，V 视频，A 音频，C 封面，D 弹幕，S 字幕，P 分P	|	|
|	--prefer	|	|	优先选择指定的媒体类型	|	见下	|
|	--reject	|	|	不选择指定的媒体类型	|	见下	|
|	--download	|	|	需要下载视频	|	获取用户投稿/收藏夹列表后默认不下载视频	|
|	--verbose	|	-v	|	输出更多log	|	可多个叠加	|
|	--quiet	|	-q	|	输出更少log	|	可多个叠加	|
|	--log	|	-l	|	输出log到文件	|	|
|	--timeout	|	-t	|	设置http超时时间	|	|
|	--stall	|	-s	|	设置请求间隔时间	|	防止请求过快导致风控，默认5秒	|
|	--bandwidth	|	-w	|	限制下载带宽	|	不适用于直播流	|
|	--interval	|	-i	|	设置查询直播间状态的间隔	|	默认30秒	|


> `--prefer` 和 `--reject` 参数为一个字符串，多个关键字之间用空格分隔，关键字越靠后权重越高


## http前端使用说明

+ index.html	目录索引，列出当前目录下的文件，对特定文件显示可用的操作
+ player.html	通用播放器，播放 FLV / HLS / m4v+m4a
+ video_page.html	视频页面，显示BV基本信息，视频分P播放，获取相关推荐BV
+ video_fetch.html	视频缓存页面，输入BV号缓存视频，显示缓存状态
+ live_page.html	直播状态页面，显示monitor.py监控的直播间状态


## 依赖表

+ M	必需依赖
+ O	可选依赖
+ UNIX 指 Linux, MacOS, BSD 等**非Windows**平台，理论上兼容Windows上的MinGW / WSL，目前仅在Linux平台上验证过
+ http前端 理论上兼容所有现代浏览器，目前仅在Firefox上验证过

### 工具模块

|	项目		|	OS	|	Python3.8+	|	httpx		|	ffprobe		|	websockets	|	brotli	|
|	----		|	----	|	----		|	----		|	----		|	----		|	----	|
|	video.py	|	UNIX	|	M		|	M		|			|			|		|
|	favlist.py	|	UNIX	|	M		|	M		|			|			|		|
|	user.py		|	UNIX	|	M		|	M		|			|			|		|
|	verify.py	|	any	|	M		|			|	O		|			|		|
|	live_rec.py	|	UNIX	|	M		|	M		|			|	O		|	O	|
|	monitor.py	|	UNIX	|	M		|	M		|			|			|		|

### http后端

|	项目		|	OS	|	Python3.8+	|	FastCGI		|	httpx		|	watchdog	|
|	----		|	----	|	----		|	----		|	----		|	----		|
|	dir_listing.py	|	any	|	M		|	M		|			|			|
|	zip_access.py	|	any	|	M		|	M		|			|			|
|	video_cache.py	|	UNIX	|	M		|	M		|	M		|	O		|
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
+ 使用 `SIGUSR1`, `SIGUSR2` 信号触发特定操作
+ 部署方案中使用了 *symbolic link*, *tmpfs*, *bind mount*


## 参考部署

### 直播录制

```sh
# 使用`cron(8)`开机启动，在tmux session中启动直播间录制
$ crontab -l
SHELL=/bin/bash
@reboot tmux new-session -d -s bili -c /PATH/TO/RECORDING/DIR "./monitor.py -v --rec-log -u bili-credential.txt -r bili-arch -i 20 --relay-root /srv/http/tmp/live/ --socket /srv/http/tmp/bili-monitor.socket rec_config.json"
```

### http 服务

```sh
# 安装软件包
sudo apt install lighttpd python3-pip python3-httpx python3-watchdog python3-websockets

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
# httpx, watchdog, websockets 也可通过pip3安装
# pip3 install --target /srv/http/fcgi --no-compile httpx watchdog websockets
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

### 直播通知

*~/bin/bili_live_notify*
```sh
#!/bin/sh
REC_SERVER=localhost
cd /PATH/TO/CODE
exec desktop/live_notify.py --monitor-url http://$REC_SERVER/api/live_status config/rec_config.json
```


*~/.config/autostart/bili_live_notify.desktop*
```ini
[Desktop Entry]
Type=Application
Exec=$HOME/bin/bili_live_notify
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name[en_US]=bili_live_notify
Name=bili_live_notify
Comment[en_US]=Bilibili Live Notifier
Comment=Bilibili Live Notifier
```


## 引用

### 相关链接

+ [Bilibili](https://www.bilibili.com) 哔哩哔哩 (゜-゜)つロ 干杯~
+ [Python](https://www.python.org/) is a programming language that lets you work quickly and integrate systems more effectively.
+ [httpx](https://www.python-httpx.org/) A next-generation HTTP client for Python.
+ [FFmpeg](https://ffmpeg.org/) A complete, cross-platform solution to record, convert and stream audio and video. 
+ [FastCGI](https://pypi.org/project/fastcgi/) FastCGI and HTTP handlers for Python's socketserver classes
+ [watchdog](https://pypi.org/project/watchdog/) Python API and shell utilities to monitor file system events.
+ [websockets](https://pypi.org/project/websockets/) An implementation of the WebSocket Protocol
+ [flv.js](https://github.com/Bilibili/flv.js) An HTML5 Flash Video (FLV) Player written in pure JavaScript without Flash.
+ [hls.js](https://github.com/video-dev/hls.js) is a JavaScript library that implements an HTTP Live Streaming client.
+ [lighttpd](https://www.lighttpd.net/) is a secure, fast, compliant, and very flexible web server that has been optimized for high-performance environments.

### 其他链接

+ [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 哔哩哔哩-API收集整理 野生API文档 不断更新中...
+ [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) 哔哩哔哩常用API调用。支持视频、番剧、用户、频道、音频等功能。
+ [MDN Web Docs](https://developer.mozilla.org/) Documenting web technologies, including CSS, HTML, and JavaScript, since 2005.
+ [RFC 3875](https://www.rfc-editor.org/rfc/rfc3875): The Common Gateway Interface (CGI) Version 1.1
+ [FastCGI Specification](https://fast-cgi.github.io/spec.html) (Unofficial)
+ [RFC 8216](https://www.rfc-editor.org/rfc/rfc8216): HTTP Live Streaming
+ [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455) The WebSocket Protocol
+ [btrfs](https://btrfs.wiki.kernel.org) is a modern copy on write (COW) filesystem for Linux aimed at implementing advanced features while also focusing on fault tolerance, repair and easy administration.


### About `ssl` module safety on `fork(2)`

+ The warning messages in Python docs

	+ https://docs.python.org/3/library/os.html#os.fork
	+ https://docs.python.org/3/library/ssl.html#multi-processing

+ The mentioned patch seems not merged into cpython. It is likely that there is no protection on the Python side.

	+ https://bugs.python.org/issue18747
	+ https://bugs.python.org/file31390/openssl_prng_atfork5.patch

+ According to the OpenSSL Wiki, OpenSSL 1.1.1 rewrote RNG and elimated this issue. For lower versions, at least the pid-based solution is in effect. As pid wrap-around is unlikely in practice, this issue should not be a great concern.

	+ https://wiki.openssl.org/index.php/Random_fork-safety

+ As a result, protecting `ssl` module against `fork(2)` is considered unnecessary and is not implemented in this project.
