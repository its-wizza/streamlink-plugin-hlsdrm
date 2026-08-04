# streamlink-plugin-hlsdrm

A [streamlink](https://github.com/streamlink/streamlink) plugin that extends the standard streamlink hls plugin to support DRM streams using SAMPLE-AES and clearkeys. 

This is a reimplementation of [hlsdrm](https://github.com/jordandalley/dispatchwrapparr/blob/main/drmplugins/hlsdrm.py) by Jordan Dalley which in turn was originally based on [streamlink-plugin-dashdrm](https://github.com/titus-au/streamlink-plugin-dashdrm).

An implementation of this plugin for dash MPD DRM streams can be found at [https://github.com/titus-au/streamlink-plugin-dashdrm](https://github.com/titus-au/streamlink-plugin-dashdrm).

# Install and Use

To use this plugin, you need to utilise streamlink's plugin [sideload](https://streamlink.github.io/latest/cli/plugin-sideloading.html) capability. Download the plugin source by cloning the repository, or just downloading the plugin file (hlsdrm.py) and either place it in your streamlink plugins sideload directory, or put in a new directory and specify the path when executing streamlink with --plugin-dir <path_of_hlsdrm.py>.

Install using git by typing:
```sh
git clone https://github.com/titus-au/streamlink-plugin-hlsdrm.git
```
To update the plugin using git, change into the directory where you had cloned the plugin, then type:
```sh
git pull
```

To make use of the plugin, add hlsdrm:// in front of the url.
```sh
streamlink --plugin-dir /path/to/hlsdrm/plugin --default-stream best --url hlsdrm://http://abc.def/xyz.m3u8
```

# Parameters

The plugin accepts a number of optional parameters:
<TABLE>
  <TR>
    <TH>Option</TH>
    <TH>Description</TH>
  </TR>
  <TR>
    <TD>--hlsdrm-decryption-key &ltkey in hex or base64&gt</TD>
    <TD>This is a comma-separated list of decryption keys to be passed to ffmpeg. Keys may be supplied either as key or kid:key. If one or more KIDs are supplied, dashdrm will automatically match keys to representations using their KIDs, allowing keys to be supplied in any order. If KID matching cannot be completed, or no KIDs are supplied, keys are assigned to streams by position:
    <LI>If only one key is given, all streams use that key.</LI>
    <LI>If two keys are given, the video stream uses the first key and all remaining streams (for example audio streams) use the second key.</LI>
    <LI>If more than two keys are given, the video stream uses the first key and subsequent streams use the remaining keys in order. If there are more streams than keys, assignment wraps back to the second key.</LI></TD>
  </TR>

</TABLE>

# Disclaimer

<LI>Use of this code to decrypt DRM is purely for academic purposes. You should not use this code for any illegal purposes and I take no responsibility for your actions</LI>
<LI>This plugin is reliant on streamlink 8.4.0 which implemented the --stream-passthrough-encrypted command line option</LI>
<LI>This plugin turns on the --stream-passthrough-encrypted option, but if key method is AES-128, we'll turn it back off and allow streamlink to handle the decryption with the embedded keys</LI>
<LI>This code has basically not been tested, so consider it pre-alpha software</LI>

