from __future__ import annotations

import binascii
import re
import base64
from dataclasses import dataclass, replace, asdict

from typing import Literal, Self, ClassVar
from urllib.parse import urlsplit

from requests import Response
from streamlink import validate

from streamlink.exceptions import PluginError, FatalPluginError
from streamlink.logger import getLogger
from streamlink.plugin import Plugin, pluginmatcher, pluginargument
from streamlink.plugin.plugin import LOW_PRIORITY, parse_params
from streamlink.session import Streamlink
from streamlink.stream.ffmpegmux import FFMPEGMuxer
from streamlink.stream.hls import (HLSStream,
                                   HLSStreamReader,
                                   HLSStreamWriter,
                                   HLSStreamWorker,
                                   MuxedHLSStream, M3U8, M3U8Parser, parse_tag)
from streamlink.stream.hls.segment import HLSSegment, Key, HLSPlaylist
from streamlink.utils.url import update_scheme

log = getLogger(__name__)

WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
ZERO_KID = "00000000000000000000000000000000"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)

HLSDRM_OPTIONS = [
    "decryption-key",
]

@pluginmatcher(
    re.compile(r"hlsdrm(?:variant)?://(?P<url>\S+)(?:\s(?P<params>.+))?$"),
)
@pluginmatcher(
    priority=LOW_PRIORITY,
    pattern=re.compile(
        # URL with explicit scheme, or URL with implicit HTTPS scheme and a path
        r"(?P<url>[^/]+/\S+\.m3u8(?:\?\S*)?)(?:\s(?P<params>.+))?$",
        re.IGNORECASE,
    ),
)
@pluginargument(
    "decryption-key",
    type="comma_list",
    help="Decryption key(s) to be passed to ffmpeg. Keys may be supplied as"
    " 'key' or 'kid:key'. When KIDs are supplied, keys are matched to"
    " streams automatically, otherwise positional matching is used."
)

class HLSPluginDRM(Plugin):
    def _get_streams(self):
        data = self.match.groupdict()
        url = update_scheme("https://", str(data.get("url", "")),
                        force=False)
        params = parse_params(data.get("params"))
        log.debug(f"URL={url}; params={params}")

        # process and store plugin options before passing streams back
        for option in HLSDRM_OPTIONS:
            if option == 'decryption-key':
                if self.get_option('decryption-key'):
                    self.session.options[option] = self._process_keys()
            else:
                self.session.options[option] = self.get_option(option)

        self.session.set_option("stream-passthrough-encrypted", True)

        return HLSStreamDRM.parse_variant_playlist(self.session, url,
                    **params)


    def _process_keys(self):
        """
            Parse user-supplied decryption keys.

            Keys may be supplied either as:

                key

            or

                kid:key

            Returns a list of tuples.
            Each tuple contains (kid, key), where kid is None if no KID was supplied.
            """
        keys = self.get_option('decryption-key')
        # if a colon separated key is given, assume its kid:key
        return_keys = []
        has_kid = False
        for k in keys:
            kid = None
            parts = k.split(':', 1)
            if len(parts) == 2:
                kid, key = parts
                has_kid = True
                kid = kid.replace("-", "").lower()
                log.debug('Decryption key %s has KID %s', key, kid)
            else:
                key = parts[-1]
            key_len = len(key)
            log.debug('Decryption Key %s has %s digits', key, key_len)
            if key_len in (21, 22, 23, 24):
                # key len of 21-24 may mean a base64 key was provided, so we
                # try and decode it
                log.debug("Decryption key length is too short to be hex and looks like it might be base64, so we'll try and decode it..")
                b64_string = key
                padding = 4 - (len(b64_string) % 4)
                b64_string = b64_string + ("=" * padding)
                b64_key = base64.urlsafe_b64decode(b64_string).hex()
                if b64_key:
                    key = b64_key
                    key_len = len(b64_key)
                    log.debug('Decryption Key (post base64 decode) is %s and has %s digits', key, key_len)
            if key_len == 32:
                # sanity check that it's a valid hex string
                try:
                    int(key, 16)
                except ValueError as err:
                    raise FatalPluginError("Expecting 128bit key in 32 hex digits, but the key contains invalid hex.")
            elif key_len != 32:
                raise FatalPluginError("Expecting 128bit key in 32 hex digits.")
            return_keys.append((kid, key))
        self.session.options["store-representation-kid"] = has_kid
        return return_keys

class FFMPEGMuxerDRM(FFMPEGMuxer):
    '''
    Inherit and extend the FFMPEGMuxer class to pass decryption keys
    to ffmpeg

    The caller supplies a list of decryption keys corresponding to the input
    streams. Each entry is either a hexadecimal decryption key or None for an
    unencrypted stream. When a key is present, it is passed to FFMPEG using the
    -decryption_key input option.
    '''
    def __init__(self, session, *streams, **options):
        keys = options.pop("keys", None) or []
        if keys and len(keys) != len(streams):
            raise PluginError(f"Decryption key count ({len(keys)}) does not match stream count ({len(streams)}).")

        if not session.options.get("ffmpeg-fout"):
            session.set_option("ffmpeg-fout", "mpegts")
        if not session.options.get("ffmpeg-copyts"):
            session.set_option("ffmpeg-copyts", True)
        super().__init__(session, *streams, **options)
        # if a decryption key is set, we rebuild the ffmpeg command list
        # to include the key before specifying the input stream
        log.debug("Keys = %s", keys)
        k = 0
        # Build new ffmpeg command list
        old_cmd = self._cmd.copy()
        self._cmd = []

        while len(old_cmd) > 0:
            cmd = old_cmd.pop(0)
            if cmd == "-i":
                _ = old_cmd.pop(0)
                if keys:
                    key = keys[k]
                    if key is not None:
                        self._cmd.extend(["-decryption_key", key])
                    k += 1
                self._cmd.extend(['-thread_queue_size', '4096'])
                self._cmd.extend([cmd, _])
            else:
                self._cmd.append(cmd)
        #self._cmd.extend(["-report"])
        log.debug("Updated ffmpeg command %s", self._cmd)

@dataclass(kw_only=True)
class KeyDRM(Key):
    key_id: str | None = None

class M3U8ParserDRM(M3U8Parser):
    @parse_tag("EXT-X-KEY")
    def parse_tag_ext_x_key(self, value: str) -> None:
        super().parse_tag_ext_x_key(value)

        if self._key:
            attr = self.parse_attributes(value)

            self._key = KeyDRM(
                **asdict(self._key),
                key_id=attr.get("KEYID"),
            )

class HLSStreamWriterDRM(HLSStreamWriter):
    reader: HLSStreamReaderDRM
    stream: HLSStreamDRM

    def _write(self, segment: HLSSegment, result: Response, is_map: bool):
        key = segment.map.key if is_map and segment.map else segment.key
        if key and key.method == "AES-128":
            log.debug("Key Method is AES-128, we will let streamlink to try and decrypt.")
            self.passthrough_encrypted = False
        super()._write(segment, result, is_map,)

class HLSStreamWorkerDRM(HLSStreamWorker):
    reader: HLSStreamReaderDRM
    writer: HLSStreamWriterDRM
    stream: HLSStreamDRM

class HLSStreamReaderDRM(HLSStreamReader):
    __worker__ = HLSStreamWorkerDRM
    __writer__ = HLSStreamWriterDRM

    worker: HLSStreamWorkerDRM
    writer: HLSStreamWriterDRM
    stream: HLSStreamDRM

class HLSStreamDRM(HLSStream):
    __shortname__ = "hlsdrm"
    __reader__: ClassVar[type[HLSStreamReaderDRM]] = HLSStreamReaderDRM
    __parser__: ClassVar[type[M3U8Parser[M3U8[HLSSegment, HLSPlaylist], HLSSegment, HLSPlaylist]]] = M3U8ParserDRM

    @classmethod
    def parse_variant_playlist(
        cls,
        session: Streamlink,
        url: str,
        name_key: str = "name",
        name_prefix: str = "",
        check_streams: bool | Literal["playlists", "segments"] = False,
        force_restart: bool = False,
        name_fmt: str | None = None,
        start_offset: float = 0,
        duration: float | None = None,
        **kwargs,
    ) -> dict[str, Self | MuxedHLSStreamDRM[Self]]:
        streams = super().parse_variant_playlist(
                            session=session,
                            url=url,
                            name_key=name_key,
                            name_prefix=name_prefix,
                            check_streams=check_streams,
                            force_restart=force_restart,
                            name_fmt=name_fmt,
                            start_offset=start_offset,
                            duration=duration,
                            **kwargs)
        if not streams:
            log.debug ('No streams')
            return {"live": MuxedHLSStreamDRM(
                            session=session,
                            video=url,
                            audio=None,
                            hlsstream=cls,
                            start_offset=start_offset,
                            duration=duration,
                            **kwargs)}

        new_streams = {}
        for name, stream in streams.items():
            if isinstance(stream, MuxedHLSStream):
                muxed_stream = MuxedHLSStreamDRM(
                                stream.session,
                                None,
                                None)
                muxed_stream.__dict__.update(stream.__dict__)
                new_streams[name] = muxed_stream
            else:
                muxed_stream = MuxedHLSStreamDRM(
                                stream.session,
                                video=stream.url,
                                audio=None,
                                hlsstream=cls,
                                multivariant=stream.multivariant,
                                start_offset=stream.start_offset,
                                duration=stream.duration,
                                **kwargs)
                new_streams[name] = muxed_stream
        return new_streams

class MuxedHLSStreamDRM(MuxedHLSStream):
    def __init__(self, session: Streamlink, video: str, audio: str | list[str], **kwargs):
        super().__init__(session, video, audio, **kwargs)
        self._kid_cache = {}

    def _resolve_decryption_keys(self, readers):
        """
        Resolve one decryption key for each input stream.

        If user-supplied KIDs are available, representations are matched by KID.
        If KID matching cannot be completed for every stream, positional key
        assignment is used instead.

        Returns a list of keys aligned with the supplied reader objects.
        """
        in_keys = self.session.options.get("decryption-key")
        if not in_keys:
            return []

        kid_lookup = {
            kid: key
            for kid, key in in_keys
            if kid is not None
        }

        def positional_match():
            keys = [key for _, key in in_keys]
            # If only 1 key is given, then we use that also for all remaining streams
            if len(keys) == 1:
                return [keys[0]] * len(readers)
            out_keys = []
            key = 0
            for _ in readers:
                out_keys.append(keys[key])
                key += 1
                # If we had more streams than keys, start with the first audio key again
                if key == len(keys):
                    key = 1
            return out_keys

        if not kid_lookup:
            log.debug("No KIDs supplied, using positional assignment")
            return positional_match()

        rtn_keys = []

        for reader in readers:
            playlist = self._fetch_playlist(self.session, reader.stream.url)

            if playlist is None:
                return positional_match()

            kid = self._resolve_stream_kid(playlist)
            if kid is None:
                log.debug("Unable to determine stream KID, falling back to positional assignment")
                return positional_match()
            log.debug("Stream KID=%s", kid)
            key = kid_lookup.get(kid)
            if key is None:
                log.debug("No supplied key matches KID %s", kid)
                return positional_match()

            rtn_keys.append(key)

        log.debug("Successfully matched all streams by KID")
        return rtn_keys

    @staticmethod
    def _fetch_playlist(session: Streamlink, url: str) -> M3U8 | None:
        try:
            res = session.http.get(url)
            parser = HLSStreamDRM.__parser__(url)
            return parser.parse(res.text)
        except Exception as err:
            log.debug("Unable to load playlist %s: %s", url, err)
            return None

    def _resolve_stream_kid(self, playlist: M3U8) -> str | None:
        seen_key_uris = set()
        seen_maps = set()

        for segment in playlist.segments:
            if segment.key:
                if getattr(segment.key, "key_id", None):
                    return segment.key.key_id.replace("-", "").lower()

                # Extract KID from EXT-X-KEY URI
                if segment.key.uri:
                    uri = segment.key.uri

                    if uri not in seen_key_uris:
                        seen_key_uris.add(uri)

                        kid = self._extract_kid_from_key_uri(uri)
                        if kid:
                            log.debug("KID extracted from key URI: %s", kid)
                            return kid

            # Extract KID from init segment
            if segment.map and segment.map.uri:
                uri = segment.map.uri
                cache_key = (uri, segment.map.byterange)

                if cache_key in seen_maps:
                    continue

                seen_maps.add(cache_key)

                if cache_key in self._kid_cache:
                    kid = self._kid_cache[cache_key]
                    if kid:
                        return kid
                    continue

                if uri.startswith("data:"):
                    _, data = uri.split(",", 1)
                    data = base64.b64decode(data)
                else:
                    request = self.session.http.get(uri)
                    data = request.content

                if segment.map.byterange:
                    start = segment.map.byterange.offset or 0
                    end = start + segment.map.byterange.range
                    data = data[start:end]

                scheme, kid = self._parse_init_segment(data)

                self._kid_cache[cache_key] = kid
                if kid:
                    return kid

        return None

    @staticmethod
    def _parse_init_segment(data: bytes) -> tuple[str | None, str | None]:
        scheme = None
        kid = None

        for sample_entry in (b"encv", b"enca", b"enct", b"encs"):

            entry = data.find(sample_entry)

            if entry == -1:
                continue

            if entry < 4:
                continue

            box_start = entry - 4
            box_size = int.from_bytes(
                data[box_start:box_start + 4],
                "big",
            )
            if box_size < 8:
                continue
            box_end = box_start + box_size
            if box_end > len(data):
                continue
            box = data[box_start:box_end]

            # Scheme
            schm = box.find(b"schm")

            if schm != -1 and schm + 12 <= len(box):
                scheme = box[schm + 8:schm + 12].decode("ascii", errors="ignore")
                log.debug("Protection scheme=%s", scheme)

            # KID
            tenc = box.find(b"tenc")

            if tenc != -1 and tenc + 28 <= len(box):
                kid = box[tenc + 12:tenc + 28].hex()

            break

        if kid == ZERO_KID:
            log.debug("Zero KID found in tenc, attempting PSSH fallback")
            kid = MuxedHLSStreamDRM._extract_kid_from_pssh_box(data)

        return scheme, kid

    @staticmethod
    def _extract_kid_from_pssh_box(data: bytes) -> str | None:
        pssh = data.find(b"pssh")

        if pssh == -1:
            return None

        if pssh + 32 > len(data):
            return None

        system_id = data[pssh + 12:pssh + 28]

        # Only Widevine supported
        if system_id != WIDEVINE_SYSTEM_ID:
            return None

        data_size = int.from_bytes(
            data[pssh + 28:pssh + 32],
            "big",
        )

        payload = data[pssh + 32:pssh + 32 + data_size]

        if len(payload) < 18:
            return None

        return payload[2:18].hex()

    @staticmethod
    def _extract_kid_from_key_uri(uri: str) -> str | None:
        parsed = urlsplit(uri)
        scheme = parsed.scheme.lower()

        if scheme == "skd":
            match = UUID_RE.search(uri)
            if match:
                return match.group(0).replace("-", "").lower()

            payload = parsed.netloc + parsed.path

            try:
                decoded = base64.b64decode(payload, validate=True)
                schema = validate.Schema(
                    validate.parse_json(),
                    dict,
                )
                data = schema.validate(decoded)
                return MuxedHLSStreamDRM._find_uuid(data)
            except (ValueError, binascii.Error, PluginError):
                pass

            log.debug("Unable to extract KID from SKD URI")
            return None

        if scheme == "data":
            try:
                metadata, payload = parsed.path.split(",", 1)
            except ValueError:
                log.debug("Unable to extract KID from data URI")
                return None

            if ";base64" not in metadata.lower():
                log.debug("Unable to extract KID from data URI")
                return None

            try:
                decoded = base64.b64decode(payload, validate=True)
            except (ValueError, binascii.Error):
                log.debug("Unable to extract KID from data URI")
                return None

            return MuxedHLSStreamDRM._extract_kid_from_pssh_box(decoded)

        log.debug("Unsupported key URI scheme for KID extraction: %s", scheme)
        return None

    @staticmethod
    def _find_uuid(obj) -> str | None:
        if isinstance(obj, str):
            uuid_match = UUID_RE.fullmatch(obj)
            if uuid_match:
                return uuid_match.group(0).replace("-", "").lower()

        elif isinstance(obj, list):
            for item in obj:
                kid = MuxedHLSStreamDRM._find_uuid(item)
                if kid:
                    return kid

        elif isinstance(obj, dict):
            for value in obj.values():
                kid = MuxedHLSStreamDRM._find_uuid(value)
                if kid:
                    return kid

        return None

    def open(self):
        fds = []
        metadata = self.options.get("metadata", {})
        maps = self.options.get("maps", [])
        # only update the maps values if they haven't been set
        update_maps = not maps
        for substream in self.substreams:
            log.debug("Opening %s substream", substream.shortname())
            if update_maps:
                maps.append(len(fds))
            fds.append(substream and substream.open())

        for i, subtitle in enumerate(self.subtitles.items()):
            language, substream = subtitle
            log.debug("Opening %s subtitle stream",
                    substream.shortname())
            if update_maps:
                maps.append(len(fds))
            fds.append(substream and substream.open())
            metadata[f"s:s:{i}"] = [f"language={language}"]

        keys = self._resolve_decryption_keys(fds)

        self.options["metadata"] = metadata
        self.options["maps"] = maps
        self.options["keys"] = keys

        return FFMPEGMuxerDRM(self.session, *fds, **self.options).open()

__plugin__ = HLSPluginDRM