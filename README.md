# Bilibili Archive Program

Utilities to download content from [Bilibili](https://www.bilibili.com)  
从[Bilibili](https://www.bilibili.com)下载内容的工具集  

### Common Dependencies
+ [Python3.8](https://docs.python.org/3.8/) or higher
+ [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) 16.1 or higher


### Suggestions

+ use [lighttpd](https://www.lighttpd.net/) to serve files
+ use [btrfs](https://btrfs.wiki.kernel.org) to store & snapshot & RAID massive video data

## Components

### bv_down

download videos, update/fix downloaded videos

Dependencies:
+ `dot` from [`graphviz`](https://graphviz.org/)
	+ Optional, draw flowchart of interactive video

### user_down

download videos of given user

### favlist_down

download videos in favorite-lists

### verify

check integrity of downloaded videos

Dependencies:
+ `ffprobe` from [`ffmpeg`](https://ffmpeg.org/)
	+ Optional, scan media files

+ [`defusedxml`](https://pypi.org/project/defusedxml/)
	+ Optional, safer XML parsing


### live_rec

record live-stream & chat of given live-room

### monitor

daemon process to auto record live-room

### build_index

build index json for videos

### server/fcgi_server.py

FastCGI service base-class

Dependencies:
+ [`FastCGI`](https://pypi.org/project/fastcgi/)

### server/zip_access.py

transparent file access in zip archive

### server/dir_listing.py

list directory contents as JSON

### server/index.html

parse JSON from `dir_listing` and show HTML page

### server/player.html

FLV/HLS video playback
+ HLS stream in ZIP file

Dependencies:
+ [flv.js](https://github.com/Bilibili/flv.js), for FLV playback
+ [hls.js](https://github.com/video-dev/hls.js), for HLS playback
