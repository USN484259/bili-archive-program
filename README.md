# Bilibili Archive Program

Utilities to download content from [Bilibili](https://www.bilibili.com)  
从[Bilibili](https://www.bilibili.com)下载内容的工具集  

### Components
|	program		|	description	|
|	----		|	----		|
|	`bv_down`	|	download videos, update/fix downloaded videos	|
|	`user_down`	|	download videos uploaded by given user	|
|	`favlist_down`	|	download videos in favorite-lists	|
|	`verify`	|	check integrity of downloaded videos	|
|	`live_rec`	|	record live-streaming of given rooms	|
|	`monitor_daemon`|	daemon process to auto record & download	|
|	`monitor_ctrl`	|	interface to change daemon config on the fly	|
|	`build_index`	|	utility to build html index for videos	|

### Mandatory Dependency
* [Python3.8](https://docs.python.org/3.8/) or higher
* [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)
* [aiofile](https://pypi.org/project/aiofile/)

### Optional Dependency
* `ffprobe` from [ffmpeg](https://ffmpeg.org/) to scan media files
* `dot` from [graphviz](https://graphviz.org/) to draw flowchart for interactive video
* [yattag](https://http://yattag.org/) to build index html
* [defusedxml](https://pypi.org/project/defusedxml/) to have safer xml parsing
* ~[btrfs](https://btrfs.wiki.kernel.org) to store & snapshot & RAID massive video data~
